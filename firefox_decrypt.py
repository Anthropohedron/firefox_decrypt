#!/usr/bin/env python
# -*- coding: utf-8 -*-

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# Disclamer: Parts of this script were taken from the great tool:
# dumpzilla at www.dumpzilla.org

import argparse
import json
import logging
import os
import sqlite3
import sys
from base64 import b64decode
from ctypes import c_uint, c_void_p, c_char_p, cast, byref, string_at
from ctypes import Structure, CDLL
from getpass import getpass
from subprocess import Popen, PIPE

try:
    # Python 3
    from urllib.parse import urlparse
except ImportError:
    # Python 2
    from urlparse import urlparse

try:
    # Python 3
    from configparser import ConfigParser
    raw_input = input
except ImportError:
    # Python 2
    from ConfigParser import ConfigParser

PY3 = sys.version_info.major > 2
LOG = None
VERBOSE = False


class NotFoundError(Exception):
    """Exception to handle situations where a credentials file is not found
    """
    pass


class Exit(Exception):
    """Exception to allow a clean exit from any point in execution
    """
    ERROR = 1
    MISSING_PROFILEINI = 2
    MISSING_SECRETS = 3
    BAD_PROFILEINI = 4
    LOCATION_NO_DIRECTORY = 5

    FAIL_LOAD_NSS = 11
    FAIL_INIT_NSS = 12
    FAIL_NSS_KEYSLOT = 13
    BAD_MASTER_PASSWORD = 15
    NEED_MASTER_PASSWORD = 16

    PASSSTORE_NOT_INIT = 20
    PASSSTORE_MISSING = 21
    PASSSTORE_ERROR = 22

    READ_GOT_EOF = 30
    MISSING_CHOICE = 31
    NO_SUCH_PROFILE = 32

    UNKNOWN_ERROR = 100
    UNEXPECTED_END = 101
    KEYBOARD_INTERRUPT = 102

    def __init__(self, exitcode):
        self.exitcode = exitcode

    def __unicode__(self):
        return "Premature program exit with exit code {0}".format(self.exitcode)


class Item(Structure):
    """struct needed to interact with libnss
    """
    _fields_ = [('type', c_uint), ('data', c_void_p), ('len', c_uint)]


class Credentials(object):
    """Base credentials backend manager
    """
    def __init__(self, db):
        self.db = db

        LOG.debug("Database location: %s", self.db)
        if not os.path.isfile(db):
            raise NotFoundError("ERROR - {0} database not found\n".format(db))

        LOG.info("Using %s for credentials.", db)

    def __iter__(self):
        pass

    def done(self):
        """Override this method if the credentials subclass needs to do any
        action after interaction
        """
        pass


class SqliteCredentials(Credentials):
    """SQLite credentials backend manager
    """
    def __init__(self, profile):
        db = profile + "/signons.sqlite"

        super(SqliteCredentials, self).__init__(db)

        self.conn = sqlite3.connect(db)
        self.c = self.conn.cursor()

    def __iter__(self):
        LOG.debug("Reading password database in SQLite format")
        self.c.execute("SELECT hostname, encryptedUsername, encryptedPassword, encType "
                       "FROM moz_logins")
        for i in self.c:
            # yields hostname, encryptedUsername, encryptedPassword, encType
            yield i

    def done(self):
        """Close the sqlite cursor and database connection
        """
        super(SqliteCredentials, self).done()

        self.c.close()
        self.conn.close()


class JsonCredentials(Credentials):
    """JSON credentials backend manager
    """
    def __init__(self, profile):
        db = profile + "/logins.json"

        super(JsonCredentials, self).__init__(db)

    def __iter__(self):
        with open(self.db) as fh:
            LOG.debug("Reading password database in JSON format")
            data = json.load(fh)

            try:
                logins = data["logins"]
            except:
                raise Exception("Unrecognized format in {0}".format(self.db))

            for i in logins:
                yield (i["hostname"], i["encryptedUsername"],
                       i["encryptedPassword"], i["encType"])


class NSSInteraction(object):
    """
    Interact with lib NSS
    """
    def __init__(self):
        self.NSS = None
        self.load_libnss()

    def load_libnss(self):
        """Load libnss into python using the CDLL interface
        """
        firefox = ""

        if os.name == "nt":
            nssname = "nss3.dll"
            firefox = r"c:\Program Files (x86)\Mozilla Firefox"
            os.environ["PATH"] = ';'.join([os.environ["PATH"], firefox])
            LOG.debug("PATH is now %s", os.environ["PATH"])

        elif os.uname()[0] == "Darwin":
            nssname = "libnss3.dylib"
            locations = [ loc for loc in [
                "/usr/local/lib/nss",
                "/usr/local/lib",
                "/opt/local/lib/nss",
                "/sw/lib/firefox",
                "/sw/lib/mozilla"
                ] if os.path.exists(os.path.join(loc, nssname)) ]
            if locations:
                firefox = locations[0]
        else:
            nssname = "libnss3.so"

        try:
            nsslib = os.path.join(firefox, nssname)
            LOG.debug("Loading NSS library from %s", nsslib)

            self.NSS = CDLL(nsslib)

        except Exception as e:
            LOG.error("Problems opening '%s' required for password decryption", nssname)
            LOG.error("Error was %s", e)
            raise Exit(Exit.FAIL_LOAD_NSS)

    def handle_error(self):
        """If an error happens in libnss, handle it and print some debug information
        """
        LOG.debug("Error during a call to NSS library, trying to obtain error info")

        error = self.NSS.PORT_GetError()
        self.NSS.PR_ErrorToString.restype = c_char_p
        self.NSS.PR_ErrorToName.restype = c_char_p
        error_str = self.NSS.PR_ErrorToString(error)
        error_name = self.NSS.PR_ErrorToName(error)

        if PY3:
            error_name = error_name.decode("utf8")
            error_str = error_str.decode("utf8")

        LOG.debug("%s: %s", error_name, error_str)

    def initialize_libnss(self, profile, password):
        """Initialize the NSS library by authenticating with the user supplied password
        """
        LOG.debug("Initializing NSS with profile path '%s'", profile)

        i = self.NSS.NSS_Init(profile.encode("utf8"))
        LOG.debug("Initializing NSS returned %s", i)

        if i != 0:
            LOG.error("Couldn't initialize NSS, maybe '%s' is not a valid profile?", profile)
            self.handle_error()
            raise Exit(Exit.FAIL_INIT_NSS)

        if password:
            LOG.debug("Retrieving internal key slot")
            p_password = c_char_p(password.encode("utf8"))
            keyslot = self.NSS.PK11_GetInternalKeySlot()
            LOG.debug("Internal key slot %s", keyslot)

            if keyslot is None:
                LOG.error("Failed to retrieve internal KeySlot")
                self.handle_error()
                raise Exit(Exit.FAIL_NSS_KEYSLOT)

            LOG.debug("Authenticating with password '%s'", password)

            i = self.NSS.PK11_CheckUserPassword(keyslot, p_password)
            LOG.debug("Checking user password returned %s", i)

            if i != 0:
                LOG.error("Master password is not correct")
                self.handle_error()
                raise Exit(Exit.BAD_MASTER_PASSWORD)
        else:
            LOG.warn("Attempting decryption with no Master Password")

    def decode_entry(self, user, passw):
        """Decrypt one entry in the database
        """
        username = Item()
        passwd = Item()
        outuser = Item()
        outpass = Item()

        username.data = cast(c_char_p(b64decode(user)), c_void_p)
        username.len = len(b64decode(user))
        passwd.data = cast(c_char_p(b64decode(passw)), c_void_p)
        passwd.len = len(b64decode(passw))

        LOG.debug("Decrypting username data '%s'", user)

        i = self.NSS.PK11SDR_Decrypt(byref(username), byref(outuser), None)
        LOG.debug("Decryption of username returned %s", i)

        if i == -1:
            LOG.error("Passwords protected by a Master Password!")
            self.handle_error()
            raise Exit(Exit.NEED_MASTER_PASSWORD)

        LOG.debug("Decrypting password data '%s'", passw)

        i = self.NSS.PK11SDR_Decrypt(byref(passwd), byref(outpass), None)
        LOG.debug("Decryption of password returned %s", i)

        if i == -1:
            # This shouldn't really happen but failsafe just in case
            LOG.error("Given Master Password is not correct!")
            self.handle_error()
            raise Exit(Exit.UNEXPECTED_END)

        user = string_at(outuser.data, outuser.len)
        passw = string_at(outpass.data, outpass.len)

        return user, passw

    def decrypt_passwords(self, profile, password, export):
        """
        Decrypt requested profile using the provided password and print out all
        stored passwords.
        """

        self.initialize_libnss(profile, password)

        # Any password in this profile store at all?
        got_password = False

        credentials = obtain_credentials(profile)

        LOG.info("Decrypting credentials")
        to_export = {}

        for host, user, passw, enctype in credentials:
            got_password = True

            # enctype informs if passwords are encrypted and protected by a master password
            if enctype:
                user, passw = self.decode_entry(user, passw)

            user = user.decode("utf8")
            passw = passw.decode("utf8")

            LOG.debug("Decoding username '%s' and password '%s' for website '%s'", user, passw, host)
            LOG.debug("Decoding username '%s' and password '%s' for website '%s'", type(user), type(passw), type(host))

            if export:
                # Keep track of web-address, username and passwords
                # If more than one username exists for the same web-address
                # the username will be used as name of the file
                address = urlparse(host)

                if address.netloc not in to_export:
                    to_export[address.netloc] = {user: passw}

                else:
                    to_export[address.netloc][user] = passw

            else:
                output = (
                    u"\nWebsite:   {0}\n".format(host),
                    u"Username: '{0}'\n".format(user),
                    u"Password: '{0}'\n".format(passw),
                )
                for line in output:
                    if PY3:
                        sys.stdout.write(line)
                    else:
                        sys.stdout.write(line.encode("utf8"))

        credentials.done()
        self.NSS.NSS_Shutdown()

        if export:
            export_pass(to_export)

        if not got_password:
            LOG.warn("No passwords found in selected profile")


def test_password_store(export):
    """Check if pass from passwordstore.org is installed
    If it is installed but not initialized, initialize it
    """
    # Nothing to do here if exporting wasn't requested
    if not export:
        LOG.debug("Skipping password store test, not exporting")
        return

    LOG.debug("Testing if password store is installed and configured")

    try:
        p = Popen(["pass"], stdout=PIPE, stderr=PIPE)
    except OSError as e:
        if e.errno == 2:
            LOG.error("Password store is not installed and exporting was requested")
            raise Exit(Exit.PASSSTORE_MISSING)
        else:
            LOG.error("Unknown error happened.")
            LOG.error("Error was %s", e)
            raise Exit(Exit.UNKNOWN_ERROR)

    out, err = p.communicate()
    LOG.debug("pass returned: %s %s", out, err)

    if p.returncode != 0:
        if 'Try "pass init"' in err:
            LOG.error("Password store was not initialized.")
            LOG.error("Initialize the password store manually by using 'pass init'")
            raise Exit(Exit.PASSSTORE_NOT_INIT)
        else:
            LOG.error("Unknown error happened when running 'pass'.")
            LOG.error("Stdout/Stderr was '%s' '%s'", out, err)
            raise Exit(Exit.UNKNOWN_ERROR)


def obtain_credentials(profile):
    """Figure out which of the 2 possible backend credential engines is available
    """
    try:
        credentials = JsonCredentials(profile)
    except NotFoundError:
        try:
            credentials = SqliteCredentials(profile)
        except NotFoundError:
            LOG.error("Couldn't find credentials file (logins.json or signons.sqlite).")
            raise Exit(Exit.MISSING_SECRETS)

    return credentials


def export_pass(to_export):
    """Export given passwords to password store

    Format of "to_export" should be:
        {"address": {"login": "password", ...}, ...}
    """
    LOG.info("Exporting credentials to password store")
    for address in to_export:
        for user, passw in to_export[address].items():
            # When more than one account exist for the same address, add
            # the login to the password identifier
            if len(to_export[address]) > 1:
                passname = u"web/{0}/{1}".format(address, user)

            else:
                passname = u"web/{0}".format(address)

            LOG.debug("Exporting credentials for '%s'", passname)

            data = u"{0}\n{1}\n".format(passw, user)

            LOG.debug("Inserting pass '%s' '%s'", passname, data)

            # NOTE --force is used. Existing passwords will be overwritten
            cmd = ["pass", "insert", "--force", "--multiline", passname]

            LOG.debug("Running command '%s' with stdin '%s'", cmd, data)

            p = Popen(cmd, stdout=PIPE, stderr=PIPE, stdin=PIPE)
            out, err = p.communicate(data.encode("utf8"))

            if p.returncode != 0:
                LOG.error("ERROR: passwordstore exited with non-zero: %s", p.returncode)
                LOG.error("Stdout/Stderr was '%s' '%s'", out, err)
                raise Exit(Exit.PASSSTORE_ERROR)

            LOG.debug("Successfully exported '%s'", passname)


def get_sections(profiles):
    """
    Returns hash of profile numbers and profile names.
    """
    sections = {}
    i = 1
    for section in profiles.sections():
        if section.startswith("Profile"):
            sections[str(i)] = profiles.get(section, "Path")
            i += 1
        else:
            continue
    return sections

def print_sections(sections, textIOWrapper=sys.stderr):
    """
    Prints all available sections to an textIOWrapper (defaults to sys.stderr)
    """
    for i in sorted(sections):
        textIOWrapper.write("{0} -> {1}\n".format(i, sections[i]))
    textIOWrapper.flush()

def ask_section(profiles, choice_arg):
    """
    Prompt the user which profile should be used for decryption
    """
    sections = get_sections(profiles)

    # Do not ask for choice if user already gave one
    if choice_arg and len(choice_arg) == 1:
        choice = choice_arg[0]
    else:
        # If only one menu entry exists, use it without prompting
        if len(sections) == 1:
            choice = "1"

        else:
            choice = None
            while choice not in sections:
                sys.stderr.write("Select the Firefox profile you wish to decrypt\n")
                print_sections(sections)
                try:
                    choice = raw_input("Choice: ")
                except EOFError as e:
                    LOG.error("Could not read Choice, got EOF")
                    raise Exit(Exit.READ_GOT_EOF)


    try:
        final_choice = sections[choice]
    except KeyError:
        LOG.error("Profile No. %s does not exist!", choice)
        raise Exit(Exit.NO_SUCH_PROFILE)

    LOG.debug("Profile selection matched %s", final_choice)

    return final_choice


def ask_password(profile, no_interactive):
    """
    Prompt for profile password
    """
    utf8 = "UTF-8"
    input_encoding = utf8 if sys.stdin.encoding in (None, 'ascii') else sys.stdin.encoding
    passmsg = "\nMaster Password for profile {}: ".format(profile)

    if sys.stdin.isatty() and not no_interactive:
        passwd = getpass(passmsg)

    else:
        # Ability to read the password from stdin (echo "pass" | ./firefox_...)
        passwd = sys.stdin.readline().rstrip("\n")

    if PY3:
        return passwd
    else:
        return passwd.decode(input_encoding)


def read_profiles(basepath, list_profiles):
    """
    Parse Firefox profiles in provided location.
    If list_profiles is true, will exit after listing available profiles.
    """
    profileini = os.path.join(basepath, "profiles.ini")

    LOG.debug("Reading profiles from %s", profileini)

    if not os.path.isfile(profileini):
        LOG.warn("profile.ini not found in %s", basepath)
        raise Exit(Exit.MISSING_PROFILEINI)

    # Read profiles from Firefox profile folder
    profiles = ConfigParser()
    profiles.read(profileini)

    LOG.debug("Read profiles %s", profiles.sections())

    if list_profiles:
        LOG.debug("Listing available profiles...")
        print_sections(get_sections(profiles), sys.stdout)
        raise Exit(0)

    return profiles


def get_profile(basepath, no_interactive, choice, list_profiles):
    """
    Select profile to use by either reading profiles.ini or assuming given
    path is already a profile
    If no_interactive is true, will not try to ask which profile to decrypt.
    choice contains the choice the user gave us as an CLI arg.
    If list_profiles is true will exits after listing all available profiles.
    """
    try:
        profiles = read_profiles(basepath, list_profiles)
    except Exit as e:
        if e.exitcode == Exit.MISSING_PROFILEINI:
            LOG.warn("Continuing and assuming '%s' is a profile location", basepath)
            profile = basepath

            if list_profiles:
                LOG.error("Listing single profiles not permitted.")
                raise

            if not os.path.isdir(profile):
                LOG.error("Profile location '%s' is not a directory", profile)
                raise
        else:
            raise
    else:
        if no_interactive:

            sections = get_sections(profiles)

            if choice and len(choice) == 1:

                try:
                    section = sections[(choice[0])]
                except KeyError:
                    LOG.error("Profile No. %s does not exist!", choice[0])
                    raise Exit(Exit.NO_SUCH_PROFILE)

            elif len(sections) == 1:
                section = sections['1']

            else:
                LOG.error("Don't know which profile to decrypt. We are in non-interactive mode and -c/--choice is missing.")
                raise Exit(Exit.MISSING_CHOICE)
        else:
            # Ask user which profile to open
            section = ask_section(profiles, choice)

        profile = os.path.join(basepath, section)

        if not os.path.isdir(profile):
            LOG.error("Profile location '%s' is not a directory. Has profiles.ini been tampered with?", profile)
            raise Exit(Exit.BAD_PROFILEINI)

    return profile


def parse_sys_args():
    """Parse command line arguments
    """

    if os.name == "nt":
        profile_path = os.path.join(os.environ['APPDATA'],
                "Mozilla", "Firefox")
    elif os.uname()[0] == "Darwin":
        profile_path = "~/Library/Application Support/Firefox"
    else:
        profile_path = "~/.mozilla/firefox"

    parser = argparse.ArgumentParser(
        description="Access Firefox/Thunderbird profiles and decrypt existing passwords"
    )
    parser.add_argument("profile", nargs='?', default=profile_path,
                        help="Path to profile folder (default: {0})".format(profile_path))
    parser.add_argument("-e", "--export-pass", action="store_true",
                        help="Export URL, username and password to pass from passwordstore.org")
    parser.add_argument("-n", "--no-interactive", action="store_true",
                        help="Disable interactivity.")
    parser.add_argument("-c", "--choice", nargs=1,
                        help="The profile to use (starts with 1). If only one profile, defaults to that.")
    parser.add_argument("-l", "--list", action="store_true",
                        help="List profiles and exit.")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="Verbosity level. Warning on -vv (highest level) user input will be printed on screen")

    args = parser.parse_args()

    return args


def setup_logging(args):
    """Setup the logging level and configure the basic logger
    """
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    else:
        level = logging.WARN

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=level,
    )

    global LOG
    LOG = logging.getLogger(__name__)


def main():
    """Main entry point
    """
    args = parse_sys_args()

    setup_logging(args)

    LOG.debug("Parsed commandline arguments: %s", args)

    # Check whether pass from passwordstore.org is installed
    test_password_store(args.export_pass)

    nss = NSSInteraction()

    basepath = os.path.expanduser(args.profile)

    # Read profiles from profiles.ini in profile folder
    profile = get_profile(basepath, args.no_interactive, args.choice, args.list)

    # Prompt for Master Password
    password = ask_password(profile, args.no_interactive)

    # And finally decode all passwords
    nss.decrypt_passwords(profile, password, args.export_pass)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt as e:
        print("Quit.")
        sys.exit(Exit.KEYBOARD_INTERRUPT)
    except Exit as e:
        sys.exit(e.exitcode)
