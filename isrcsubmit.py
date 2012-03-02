#!/usr/bin/env python2
# Copyright (C) 2010-2012 Johannes Dewender
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""This is a tool to submit ISRCs from a disc to MusicBrainz.

Icedax is used to gather the ISRCs and python-musicbrainz2 to submit them.
The project is hosted on
https://github.com/JonnyJD/musicbrainz-isrcsubmit
and the script is als available on
http://kraehen.org/isrcsubmit.py
"""

isrcsubmitVersion = "0.3.1"
agentName = "isrcsubmit-jonnyjd-" + isrcsubmitVersion
backends = ["cdda2wav", "icedax"]       # starting with highest priority
packages = {"cdda2wav": "cdrtools", "icedax": "cdrkit"}

import getpass
import sys
import os
import re
from subprocess import Popen, PIPE
from distutils.version import StrictVersion
from musicbrainz2 import __version__ as musicbrainz2_version
from musicbrainz2.disc import readDisc, DiscError, getSubmissionUrl
from musicbrainz2.model import Track
from musicbrainz2.webservice import WebService, Query
from musicbrainz2.webservice import ReleaseFilter, ReleaseIncludes
from musicbrainz2.webservice import RequestError, AuthenticationError
from musicbrainz2.webservice import ConnectionError, WebServiceError

scriptname = os.path.basename(sys.argv[0])

def print_usage(scriptname):
    print
    print "usage:", scriptname, "[-d] USERNAME [DEVICE]"
    print
    print " -d, --debug\tenable debug messages"
    print " -h, --help\tprint usage and multi-disc information"
    print

help_text = \
"""A note on Multi-disc-releases:

Isrcsubmit uses the MusicBrainz web service version 1.
This api is not tailored for MusicBrainz NGS (Next Generation Schema) and expects to have one release per disc. So it does not know which tracks are on a specific disc and lists all tracks in the overall release.
In order to attach the ISRCs to the correct tracks an offset is necessary for multi-disc-releases. For the first disc and last disc this can be guessed easily. Starting with 3 discs irscsubmit will ask you for the offset of the "middle discs".
The offset is the sum of track counts on all previous discs.

Example:
    disc 1: (13 tracks)
    disc 2: (17 tracks)
    disc 3:  19 tracks (current disc)
    disc 4: (23 tracks)
    number of tracks altogether: 72

The offset we have to use is 30 (= 13 + 17)

Isrcsubmit only knows how many tracks the current disc has and the total number of tracks on the release given by the web service. So the offset must be between 0 and 53 (= 72 - 19), which is the range isrcsubmit lets you choose from.

The number of discs in the release and the position of this disc give by isrcsubmit is not necessarily correct. There can be multiple disc IDs per actual disc. You should only count tracks on your actual discs.
Isrcsubmit can give you a link for an overview of the disc IDs for your release.

Isrcsubmit will warn you if there are any problems and won't actually submit anything to MusicBrainz without giving a final choice.


Isrcsubmit will warn you if any duplicate ISRCs are detected and help you fix priviously inserted duplicate ISRCs.
The ISRC-track relationship we found on our disc is taken as our correct evaluation.


Please report bugs on https://github.com/JonnyJD/musicbrainz-isrcsubmit"""


class Isrc(object):
    def __init__(self, isrc, track=None):
        self._id = isrc
        self._tracks = []
        if track is not None:
            self._tracks.append(track)

    def addTrack(self, track):
        if track not in self._tracks:
            self._tracks.append(track)

    def getTracks(self):
        return self._tracks

    def getTrackNumbers(self):
        numbers = []
        for track in self._tracks:
            numbers.append(track.getNumber())
        return ", ".join(map(str, numbers))


class EqTrack(Track):
    """track with equality checking

    This makes it easy to check if this track is already in a collection.
    Only the element already in the collection needs to be hashable.

    """
    def __init__(self, track):
        self._track = track

    def __eq__(self, other):
        return self.getId() == other.getId()

    def getId(self):
        return self._track.getId()

    def getArtist(self):
        return self._track.getArtist()

    def getTitle(self):
        return self._track.getTitle()

    def getISRCs(self):
        return self._track.getISRCs()

class NumberedTrack(EqTrack):
    """A track found on an analyzed (own) disc

    """
    def __init__(self, track, number):
        self._track = track
        self._number = number

    def getNumber(self):
        """The track number on the analyzed disc"""
        return self._number

class OwnTrack(NumberedTrack):
    """A track found on an analyzed (own) disc

    """
    pass

def get_prog_version(prog):
    if prog == "icedax":
        return Popen([prog, "--version"], stderr=PIPE).communicate()[1].strip()
    elif prog == "cdda2wav":
        outdata = Popen([prog, "-version"], stdout=PIPE).communicate()[0]
        return " ".join(outdata.splitlines()[0].split()[0:2])
    else:
        return prog


def has_backend(backend):
    devnull = open(os.devnull, "w")
    p_which = Popen(["which", backend], stdout=PIPE, stderr=devnull)
    backend_path = p_which.communicate()[0].strip()
    if p_which.returncode == 0:
        # check if it is only a symlink to another backend
        real_backend = os.path.basename(os.path.realpath(backend_path))
        if backend != real_backend and real_backend in backends: 
            return False # use real backend instead, or higher priority
        return True
    else:
        return False

def askForOffset(discTrackCount, releaseTrackCount):
    print
    print "How many tracks are on the previous (actual) discs altogether?"
    num = raw_input("[0-%d] " % (releaseTrackCount - discTrackCount))
    return int(num)

def printError(*args):
    stringArgs = tuple(map(str, args))
    msg = " ".join(("ERROR:",) + stringArgs)
    sys.stderr.write(msg + "\n")

def printError2(*args):
    stringArgs = tuple(map(str, args))
    msg = " ".join(("      ",) + stringArgs)
    sys.stderr.write(msg + "\n")

def gatherIsrcs(backend):
    backend_output = []

    if backend in ["cdda2wav", "icedax"]:
        # getting the ISRCs with icedax
        try:
            p1 = Popen([backend, '-J', '-H', '-D', device], stderr=PIPE)
            p2 = Popen(['grep', 'ISRC'], stdin=p1.stderr, stdout=PIPE)
            isrcout = p2.communicate()[0]
        except:
            printError("Couldn't gather ISRCs with icedax and grep!")
            sys.exit(1)
        pattern = \
            'T:\s+([0-9]+)\sISRC:\s+([A-Z]{2})-?([A-Z0-9]{3})-?(\d{2})-?(\d{5})'
        for line in isrcout.splitlines():
            if debug: print line
            for text in line.splitlines():
                if text.startswith("T:"):
                    m = re.search(pattern, text)
                    if m == None:
                        print "can't find ISRC in:", text
                        continue
                    # found an ISRC
                    trackNumber = int(m.group(1))
                    isrc = m.group(2) + m.group(3) + m.group(4) + m.group(5)
                    backend_output.append((trackNumber, isrc))

    return backend_output

def cleanupIsrcs(isrcs):
    for isrc in isrcs:
        tracks = isrcs[isrc].getTracks()
        if len(tracks) > 1:
            print
            print "ISRC", isrc, "attached to:"
            for track in tracks:
                print "\t",
                artist = track.getArtist()
                string = ""
                if artist:
                    string += artist.getName() + " - "
                string += track.getTitle()
                print string,
                # tab alignment
                if len(string) >= 32:
                    print
                    print " " * 40,
                else:
                    if len(string) < 7:
                        print "\t",
                    if len(string) < 15:
                        print "\t",
                    if len(string) < 23:
                        print "\t",
                    if len(string) < 31:
                        print "\t",

                # append track# and evaluation, if available
                if isinstance(track, NumberedTrack):
                    print "\t track", track.getNumber(),
                if isinstance(track, OwnTrack):
                    print "   [OUR EVALUATION]"
                else:
                    print

            url = "http://musicbrainz.org/isrc/" + isrc
            if raw_input("Open ISRC in firefox? [Y/n] ") != "n":
                os.spawnlp(os.P_NOWAIT, "firefox", "firefox", url)
                raw_input("(press <return> when done with this ISRC) ")


print "isrcsubmit", isrcsubmitVersion, "by JonnyJD for MusicBrainz"

# gather arguments
if len(sys.argv) < 2 or len(sys.argv) > 4:
    print_usage(scriptname)
    sys.exit(1)
else:
    # defaults
    debug = False
    username = None
    device = "/dev/cdrom"
    for i in range(1, len(sys.argv)):
        arg = sys.argv[i]
        if arg == "-d" or arg == "--debug":
            debug = True
        elif arg == "-h" or arg == "--help":
            print_usage()
            print
            print help_text
            sys.exit(0)
        elif username == None:
            username = arg
        else:
            device = arg

print "using python-musicbrainz2", musicbrainz2_version
if StrictVersion(musicbrainz2_version) < "0.7.0":
    printError("Your version of python-musicbrainz2 is outdated")
    printError2("You WILL NOT be able to even check ISRCs")
    printError2("Please use AT LEAST python-musicbrainz2 0.7.0")
    sys.exit(-1) # the script can't do anything useful
if StrictVersion(musicbrainz2_version) < "0.7.3":
    printError("Cannot use AUTH DIGEST")
    printError2("You WILL NOT be able to submit ISRCs -> check-only")
    printError2("Please use python-musicbrainz2 0.7.3 or higher")
    # do not exit, check-only is what happens most of the times anyways
# We print two warnings for clients between 0.7.0 and 0.7.3,
# because 0.7.4 is important. (-> no elif)
if StrictVersion(musicbrainz2_version) < "0.7.4":
    print "WARNING: Cannot set userAgent"
    print "         You WILL have random connection problems due to throttling"
    print "         Please use python-musicbrainz2 0.7.4 or higher"
    print

# search for backend
backend = None
for prog in backends:
    if has_backend(prog):
        backend = prog
        break

if backend is None:
    verbose_backends = []
    for program in backends:
        if program in packages:
            verbose_backends.append(program + " (" + packages[program] + ")")
        else:
            verbose_backends.append(program)
    printError("Cannot find a backend to extract the ISRCS!")
    printError2("Isrcsubmit can work with one of the following:")
    printError2("  " + ", ".join(verbose_backends))
    sys.exit(-1)
else:
    print "using", get_prog_version(backend)

print
print "Please input your Musicbrainz password"
password = getpass.getpass('Password: ')
print

try:
    # get disc ID
    disc = readDisc(deviceName=device)
except DiscError, e:
    printError("DiscID calculation failed:", str(e))
    sys.exit(1)

discId = disc.getId()
discTrackCount = len(disc.getTracks())

print 'DiscID:\t\t', discId
print 'Tracks on Disc:\t', discTrackCount

# connect to the server
if StrictVersion(musicbrainz2_version) >= "0.7.4":
    # There is a warning printed above, when < 0.7.4
    service = WebService(username=username, password=password,
            userAgent=agentName)
else:
    # standard userAgent: python-musicbrainz/__version__
    service = WebService(username=username, password=password)

# This clientId is currently only used for submitPUIDs and submitCDStub
# which we both don't do directly.
q = Query(service, clientId=agentName)

# searching for release
discId_filter = ReleaseFilter(discId=discId)
try:
    results = q.getReleases(filter=discId_filter)
except ConnectionError, e:
    printError("Couldn't connect to the Server:", str(e))
    sys.exit(1)
except WebServiceError, e:
    printError("Couldn't fetch release:", str(e))
    sys.exit(1)
if len(results) == 0:
    print "This Disc ID is not in the Database."
    url = getSubmissionUrl(disc)
    print "Would you like to open Firefox to submit it?",
    if raw_input("[y/N] ") == "y":
        try:
            os.execlp('firefox', 'firefox', url)
        except OSError, e:
            printError("Couldn't open the url in firefox:", str(e))
            printError2("Please submit it via:", url)
            sys.exit(1)
    else:
        print "Please submit the Disc ID it with this url:"
        print url
        sys.exit(1)

elif len(results) > 1:
    print "This Disc ID is ambiguous:"
    for i in range(len(results)):
        release = results[i].release
        print str(i)+":", release.getArtist().getName(),
        print "-", release.getTitle(),
        print "(" + release.getTypes()[1].rpartition('#')[2] + ")"
        events = release.getReleaseEvents()
        for event in events:
            country = (event.getCountry() or "").ljust(2)
            date = (event.getDate() or "").ljust(10)
            barcode = (event.getBarcode() or "").rjust(13)
            print "\t", country, "\t", date, "\t", barcode
    num =  raw_input("Which one do you want? [0-%d] " % i)
    result = results[int(num)]
    print
else:
    result = results[0]

# getting release details
releaseId = result.getRelease().getId()
include = ReleaseIncludes(artist=True, tracks=True, isrcs=True, discs=True)
try:
    release = q.getReleaseById(releaseId, include=include)
except ConnectionError, e:
    printError("Couldn't connect to the Server:", str(e))
    sys.exit(1)
except WebServiceError, e:
    printError("Couldn't fetch release:", str(e))
    sys.exit(1)

tracks = release.getTracks()
releaseTrackCount = len(tracks)
discs = release.getDiscs()
# discCount is actually the count of DiscIDs
# there can be multiple DiscIDs for a single disc
discIdCount = len(discs)
print 'Artist:\t\t', release.getArtist().getName()
print 'Release:\t', release.getTitle()
if releaseTrackCount != discTrackCount:
    # a track count mismatch due to:
    # a) multiple discs in the release
    # b) multiple DiscIDs for a single disc
    # c) a)+b)
    # d) unknown (see CRITICAL below)
    print "Tracks in Release:", releaseTrackCount
    if discIdCount > 1:
        # Handling of multiple discs in the release:
        # We can only get the overall release from MB
        # and not the Medium itself.
        # This changed with NGS. Before there was one MB release per disc.
        print
        print "WARNING: Multi-disc-release given by web service."
        print "See '" + scriptname, "-h' for help"
        print "Discs (or disc IDs) in Release: ", discIdCount
        for i in range(discIdCount):
            print "\t", discs[i].getId(),
            if discs[i].getId() == discId:
                discIdNumber = i + 1
                print "[THIS DISC]"
            else:
                print
        print "There might be multiple disc IDs per disc"
        print "so the number of actual discs could be lower."
        print
        print "This is disc (ID)", discIdNumber, "of", discIdCount
        if discIdNumber == 1:
            # the first disc never needs an offset
            trackOffset = 0
            print "Guessing track offset as", trackOffset
        elif discIdNumber == discIdCount:
            # It is easy to guess the offset when this is the last disc,
            # because we have no unknown track counts after this.
            trackOffset = releaseTrackCount - discTrackCount
            print "Guessing track offset as", trackOffset
        else:
            # For "middle" discs we have unknown track numbers
            # before and after the current disc.
            # -> the user has to tell us an offset to use
            print "Cannot guess the track offset."

            # There can also be multiple discIds for one disc of the release
            # so we give a MB-link to help which IDs
            # belong to which disc of the release.
            # We can't provide that ourselfes without making
            # many requests to MB or using the new web-api 2.
            url = releaseId + "/discids" # The "releaseId" is an url itself
            print "This url would provide some info about the disc IDs:"
            print url
            print "Would you like to open it in Firefox?",
            if raw_input("[y/N] ") == "y":
                try:
                    os.spawnlp(os.P_NOWAIT, 'firefox', 'firefox', url)
                except OSError, e:
                    printError("Couldn't open the url in firefox:", str(e))

            trackOffset = askForOffset(discTrackCount, releaseTrackCount)
    else:
        # This is actually a weird case
        # Having only 1 disc, but not matching trackCounts
        # Possibly some data/video track,
        # but these should be suppressed on both ends the same
        print "CRITICAL: track count mismatch!"
        print "CRITICAL: There are", discTrackCount, "tracks on the disc,"
        print "CRITICAL: but", releaseTrackCount,
        print "tracks on a SINGLE-disc-release."
        print "CRITICAL: This is not supposed to happen."
        sys.exit(-1)
else:
    # the track count matches
    trackOffset = 0

print
# Extract ISRCs
backend_output = gatherIsrcs(backend) # (track, isrc)

# prepare to add the ISRC we found to the corresponding track
# and check for local duplicates now and server duplicates later
isrcs = dict()          # isrcs found on disc
tracks2isrcs = dict()   # isrcs to be submitted
for (trackNumber, isrc) in backend_output:
    if isrc not in isrcs:
        isrcs[isrc] = Isrc(isrc)
        # check if we found this ISRC for multiple tracks
        with_isrc = filter(lambda item: item[1] == isrc, backend_output)
        if len(with_isrc) > 1:
            listOfTracks = map(str, map(lambda l: l[0], with_isrc))
            printError(backend + " gave the same ISRC for multiple tracks!")
            printError2("ISRC:", isrc, "\ttracks:", ", ".join(listOfTracks))
    try:
        track = tracks[trackNumber + trackOffset - 1]
        ownTrack = OwnTrack(track, trackNumber)
        isrcs[isrc].addTrack(ownTrack)
        # check if the ISRC was already added to the track
        if isrc not in track.getISRCs():
            tracks2isrcs[track.getId()] = isrc
            print "found new ISRC for track",
            print str(trackNumber) + ":", isrc
        else:
            print isrc, "is already attached to track", trackNumber
    except IndexError, e:
        printError("ISRC", isrc, "found for unknown track", trackNumber)
for isrc in isrcs:
    for track in isrcs[isrc].getTracks():
        trackNumber = track.getNumber()

print
# try to submit the ISRCs
update_intention = True
if len(tracks2isrcs) == 0:
    print "No new ISRCs could be found."
else:
    if raw_input("Is this correct? [y/N] ") == "y":
        try:
            q.submitISRCs(tracks2isrcs)
            print "Successfully submitted", len(tracks2isrcs), "ISRCs."
        except RequestError, e:
            printError("Invalid Request:", str(e))
        except AuthenticationError, e:
            printError("Invalid Credentials:", str(e))
        except WebServiceError, e:
            printError("Couldn't send ISRCs:", str(e))
    else:
        update_intention = False
        print "Nothing was submitted to the server."

# check for overall duplicate ISRCs, including server provided
if update_intention:
    duplicates = 0
    # add already attached ISRCs
    for i in range(0, len(tracks)):
        track = tracks[i]
        if i in range(trackOffset, trackOffset + discTrackCount):
            trackNumber = i - trackOffset + 1
            track = NumberedTrack(track, trackNumber)
        for isrc in track.getISRCs():
            # only check ISRCS we also found on our disc
            if isrc in isrcs:
                isrcs[isrc].addTrack(track)
    # check if we have multiple tracks for one ISRC
    for isrc in isrcs:
        if len(isrcs[isrc].getTracks()) > 1:
            duplicates += 1

    if duplicates > 0:
        print
        print "There were", duplicates, "ISRCs",
        print "that are attached to multiple tracks on this release."
        if raw_input("Do you want to help clean those up? [y/N] ") == "y":
            cleanupIsrcs(isrcs)

# vim:set shiftwidth=4 smarttab expandtab:
