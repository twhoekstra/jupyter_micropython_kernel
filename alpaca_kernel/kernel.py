import ast
import base64
import binascii
import logging
import os
import re
import traceback
import time
import urllib
import uuid
from io import BytesIO

import argparse  # use of argparse for handling the %commands in the cells
import shlex  # use of shlex for handling the %commands in the cells
import matplotlib.pyplot as plt
import numpy as np
import websocket  # only for WebSocketConnectionClosedException
from ipykernel.kernelbase import Kernel

from . import deviceconnector

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --------------------- Plotting settings ----------------------------
DEFAULT_PLOT_MODE = 1  # 0 = no plot, 1 = matplotlib plot, 2 = live plot, 3 scope

# --------- Constants for plotting in matplotlib style ---------------
# Format for string is {dictionary of settings}[[x axis], [y axis]]
VALID_KEYS = ['color', 'linestyle', 'linewidth', 'marker', 'label']
# Format for attribute is ATTRIBUTE_PREFIXattribute(parameters)

VALID_ATTRIBUTES = {'legend': 'legend',
                    'axhline': 'axhline',
                    'axvline': 'axvline',
                    'hlines': 'hlines',
                    'vlines': 'vlines',
                    'grid': 'grid',
                    'xlim': 'set_xlim',
                    'ylim': 'set_ylim',
                    'xlabel': 'set_xlabel',
                    'ylabel': 'set_ylabel',
                    'title': 'set_title'}
# Key: accepted input, Value: function to run as ouput
ATTRIBUTE_PREFIX = '%matplotlib --'  # Prefix to recognize attribute
PLOT_PREFIX = '%matplotlibdata --'

# --------------------------------------------------------------------

serialtimeout = 0.5
serialtimeoutcount = 10

ap_plot = argparse.ArgumentParser(prog="%plot", add_help=False)
ap_plot.add_argument('--mode', type=str, default='matplotlib')
ap_plot.add_argument('--trigger_lvl', type=float, default=1.0)
ap_plot.add_argument('--type', type=str, default='RISE')
ap_plot.add_argument('--chan', type=int, default=1)

ap_bypass = argparse.ArgumentParser(prog="%python", add_help=False)

ap_serialconnect = argparse.ArgumentParser(prog="%serialconnect", add_help=False)
ap_serialconnect.add_argument('--raw', help='Just open connection', action='store_true')
ap_serialconnect.add_argument('--port', type=str, default=0)
ap_serialconnect.add_argument('--baud', type=int, default=115200)
ap_serialconnect.add_argument('--verbose', action='store_true')

ap_socketconnect = argparse.ArgumentParser(prog="%socketconnect", add_help=False)
ap_socketconnect.add_argument('--raw', help='Just open connection', action='store_true')
ap_socketconnect.add_argument('ipnumber', type=str)
ap_socketconnect.add_argument('portnumber', type=int)

ap_disconnect = argparse.ArgumentParser(prog="%disconnect", add_help=False)
ap_disconnect.add_argument('--raw', help='Close connection without exiting paste mode', action='store_true')

ap_websocketconnect = argparse.ArgumentParser(prog="%websocketconnect", add_help=False)
ap_websocketconnect.add_argument('--raw', help='Just open connection', action='store_true')
ap_websocketconnect.add_argument('websocketurl', type=str, default="ws://192.168.4.1:8266", nargs="?")
ap_websocketconnect.add_argument("--password", type=str)
ap_websocketconnect.add_argument('--verbose', action='store_true')

ap_writebytes = argparse.ArgumentParser(prog="%writebytes", add_help=False)
ap_writebytes.add_argument('--binary', '-b', action='store_true')
ap_writebytes.add_argument('--verbose', '-v', action='store_true')
ap_writebytes.add_argument('stringtosend', type=str)

ap_readbytes = argparse.ArgumentParser(prog="%readbytes", add_help=False)
ap_readbytes.add_argument('--binary', '-b', action='store_true')

ap_sendtofile = argparse.ArgumentParser(prog="%sendtofile",
                                        description="send a file to the microcontroller's file system", add_help=False)
ap_sendtofile.add_argument('--append', '-a', action='store_true')
ap_sendtofile.add_argument('--mkdir', '-d', action='store_true')
ap_sendtofile.add_argument('--binary', '-b', action='store_true')
ap_sendtofile.add_argument('--execute', '-x', action='store_true')
ap_sendtofile.add_argument('--source', help="source file", type=str, default="<<cellcontents>>", nargs="?")
ap_sendtofile.add_argument('--quiet', '-q', action='store_true')
ap_sendtofile.add_argument('--QUIET', '-Q', action='store_true')
ap_sendtofile.add_argument('destinationfilename', type=str, nargs="?")

ap_ls = argparse.ArgumentParser(prog="%ls", description="list directory of the microcontroller's file system",
                                add_help=False)
ap_ls.add_argument('--recurse', '-r', action='store_true')
ap_ls.add_argument('dirname', type=str, nargs="?")

ap_fetchfile = argparse.ArgumentParser(prog="%fetchfile",
                                       description="fetch a file from the microcontroller's file system",
                                       add_help=False)
ap_fetchfile.add_argument('--binary', '-b', action='store_true')
ap_fetchfile.add_argument('--print', '-p', action="store_true")
ap_fetchfile.add_argument('--load', '-l', action="store_true")
ap_fetchfile.add_argument('--quiet', '-q', action='store_true')
ap_fetchfile.add_argument('--QUIET', '-Q', action='store_true')
ap_fetchfile.add_argument('sourcefilename', type=str)
ap_fetchfile.add_argument('destinationfilename', type=str, nargs="?")

ap_mpycross = argparse.ArgumentParser(prog="%mpy-cross", add_help=False)
ap_mpycross.add_argument('--set-exe', type=str)
ap_mpycross.add_argument('pyfile', type=str, nargs="?")

ap_esptool = argparse.ArgumentParser(prog="%esptool", add_help=False)
ap_esptool.add_argument('--port', type=str, default=0)
ap_esptool.add_argument('espcommand', choices=['erase', 'esp32', 'esp8266'])
ap_esptool.add_argument('binfile', type=str, nargs="?")

ap_capture = argparse.ArgumentParser(prog="%capture", description="capture output printed by device and save to a file",
                                     add_help=False)
ap_capture.add_argument('--quiet', '-q', action='store_true')
ap_capture.add_argument('--QUIET', '-Q', action='store_true')
ap_capture.add_argument('outputfilename', type=str)

ap_writefilepc = argparse.ArgumentParser(prog="%%writefile", description="write contents of cell to file on PC",
                                         add_help=False)
ap_writefilepc.add_argument('--append', '-a', action='store_true')
ap_writefilepc.add_argument('--execute', '-x', action='store_true')
ap_writefilepc.add_argument('destinationfilename', type=str)


def parseap(ap, percentstringargs1):
    try:
        return ap.parse_known_args(percentstringargs1)[0]
    except SystemExit:  # argparse throws these because it assumes you only want to do the command line
        return None  # should be a default one


### HELPER FUNCTIONS ###s

def _to_png(fig):
    """Return a base64-encoded PNG from a
    matplotlib figure."""
    imgdata = BytesIO()
    fig.savefig(imgdata, format='png')
    imgdata.seek(0)
    return urllib.parse.quote(
        base64.b64encode(imgdata.getvalue()))


def string_to_numpy(string):
    line_Items = []
    width = None
    for line in string.split("],"):
        line_Parts = line.split()
        n = len(line_Parts)
        if n == 0:
            continue
        if width is None:
            width = n
        else:
            assert n == width, "Invalid Array"
        line = line.split("[")[-1].split("]")[0]

        line_Items.append(np.fromstring(line, dtype=float, sep=','))
    return np.array(line_Items)


def string_is_array(string):
    if string.count('[') != string.count(']'):
        return False
    if sum(cc.isalpha() for cc in string) > 0:  # cant contain alphanumerics
        return False

    number_of_numbers = 0
    number_flag = False
    for cc in string:
        if cc.isnumeric() and not number_flag:  # recognize start of number
            number_flag = True
        if number_flag and cc in [']', ',']:  # recognize end of number
            number_flag = False
            number_of_numbers += 1

    if number_of_numbers != string.count(',') + 1:
        return False

    return True


def unpack_Thonny_string(output):
    ii_label_start = 0
    ii_number_start = 0
    ii_number_end = 0
    points = {}

    number_flag = False
    for ii, cc in enumerate(output):
        # Previous end is new start
        if cc.isnumeric() and output[ii - 1] == ' ' and not number_flag:  # recognize start of number
            number_flag = True
            ii_number_start = ii

        at_end = ii == len(output) - 1
        if number_flag and (cc in [' '] or at_end):  # recognize end of number
            ii_number_end = ii

            if at_end:
                ii_number_end = ii + 1

            print(ii_label_start, ii_number_start, ii_number_end)
            label = output[ii_label_start:ii_number_start].split(':')[0]
            label = label.rstrip()
            number = output[ii_number_start:ii_number_end]
            points[label] = float(number)

            # Prep for new loop
            number_flag = False
            ii_label_start = ii_number_end + 1

    return points


# Complete streaming of data to file with a quiet mode (listing number of lines)
# Set this up for pulse reading and plotting in a second jupyter page

# argparse to say --binary in the help for the tag

# 2. Complete the implementation of websockets on ESP32  -- nearly there
# 3. Create the streaming of pulse measurements to a simple javascript frontend and listing
# 4. Try implementing ESP32 webrepl over these websockets using exec()
# 6. Finish debugging the IR codes


# * upgrade picoweb to handle jpg and png and js
# * code that serves a websocket to a browser from picoweb

# then make the websocket from the ESP32 as well
# then make one that serves out sensor data just automatically
# and access and read that from javascript
# and get the webserving of webpages (and javascript) also to happen


# should also handle shell-scripting other commands, like arpscan for mac address to get to ip-numbers

# compress the websocket down to a single straightforward set of code
# take 1-second of data (100 bytes) and time the release of this string
# to the web-browser


class ALPACAKernel(Kernel):
    implementation = 'alpaca_kernel'
    implementation_version = "v0.4.2"

    banner = "MicroPython Serializer for ALPACA"

    language_info = {'name': 'python',
                     "codemirror_mode": 'python',
                     "nbconvert_exporter": "python",
                     'file_extension': '.py'}

    def __init__(self, **kwargs):
        Kernel.__init__(self, **kwargs)

        self.silent = False
        self.dc = deviceconnector.DeviceConnector(self.sres, self.sresSYS, self.sresPLOT)
        self.mpycrossexe = None

        self.srescapturemode = 0  # 0 none, 1 print lines, 2 print on-going line count (--quiet), 3 print only final line count (--QUIET)
        self.srescapturedoutputfile = None  # used by %capture command
        self.srescapturedlinecount = 0
        self.srescapturedlasttime = 0  # to control the frequency of capturing reported

        self.sresplotmode = DEFAULT_PLOT_MODE
        self.sres_trigger_lvl = 1.0
        self.sres_trig_RISE = True
        self.sres_trig_cldwn = True
        self.sres_has_trig = False
        self.sres_trig_chan = 1
        self.sresstartedplot = 0  #
        self.sresliveiteration = 0
        self.bypass = False

    def interpretpercentline(self, percentline, cellcontents):
        try:
            percentstringargs = shlex.split(percentline)
        except ValueError as e:
            self.sres("\n\n***Bad percentcommand [%s]\n" % str(e), 31)
            self.sres(percentline)
            return None

        percentcommand = percentstringargs[0]

        if percentcommand == ap_serialconnect.prog:
            apargs = parseap(ap_serialconnect, percentstringargs[1:])

            self.dc.disconnect(apargs.verbose)
            self.dc.serialconnect(apargs.port, apargs.baud, apargs.verbose)
            if self.dc.workingserial:
                if not apargs.raw:
                    if self.dc.enterpastemode(verbose=apargs.verbose):
                        self.sresSYS("Ready.\n")
                    else:
                        self.sres("Disconnecting [paste mode not working]\n", 31)
                        self.dc.disconnect(verbose=apargs.verbose)
                        self.sresSYS("  (You may need to reset the device)")
                        cellcontents = ""
            else:
                cellcontents = ""
            return cellcontents.strip() and cellcontents or None

        if percentcommand == ap_websocketconnect.prog:
            apargs = parseap(ap_websocketconnect, percentstringargs[1:])
            if apargs.password is None and not apargs.raw:
                self.sres(ap_websocketconnect.format_help())
                return None
            self.dc.websocketconnect(apargs.websocketurl)
            if self.dc.workingwebsocket:
                self.sresSYS("** WebSocket connected **\n", 32)
                if not apargs.raw:
                    pline = self.dc.workingwebsocket.recv()
                    self.sres(pline)
                    if pline == 'Password: ' and apargs.password is not None:
                        self.dc.workingwebsocket.send(apargs.password)
                        self.dc.workingwebsocket.send("\r\n")
                        res = self.dc.workingserialreadall()
                        self.sres(res)  # '\r\nWebREPL connected\r\n>>> '
                        if not apargs.raw:
                            if self.dc.enterpastemode(apargs.verbose):
                                self.sresSYS("Ready.\n")
                            else:
                                self.sres("Disconnecting [paste mode not working]\n", 31)
                                self.dc.disconnect(verbose=apargs.verbose)
                                self.sres("  (You may need to reset the device)")
                                cellcontents = ""
            else:
                cellcontents = ""
            return cellcontents.strip() and cellcontents or None

        # this is the direct socket kind, not attached to a webrepl
        if percentcommand == ap_socketconnect.prog:
            apargs = parseap(ap_socketconnect, percentstringargs[1:])
            self.dc.socketconnect(apargs.ipnumber, apargs.portnumber)
            if self.dc.workingsocket:
                self.sres("\n ** Socket connected **\n\n", 32)
                if apargs.verbose:
                    self.sres(str(self.dc.workingsocket))
                self.sres("\n")
                # if not apargs.raw:
                #    self.dc.enterpastemode()
            return cellcontents.strip() and cellcontents or None

        if percentcommand == ap_esptool.prog:
            apargs = parseap(ap_esptool, percentstringargs[1:])
            if apargs and (apargs.espcommand == "erase" or apargs.binfile):
                self.dc.esptool(apargs.espcommand, apargs.port, apargs.binfile)
            else:
                self.sres(ap_esptool.format_help())
                self.sres("Please download the bin file from https://micropython.org/download/#{}".format(
                    apargs.espcommand if apargs else ""))
            return cellcontents.strip() and cellcontents or None

        if percentcommand == ap_writefilepc.prog:
            apargs = parseap(ap_writefilepc, percentstringargs[1:])
            if apargs:
                if apargs.append:
                    self.sres("Appending to {}\n\n".format(apargs.destinationfilename), asciigraphicscode=32)
                    fout = open(apargs.destinationfilename, ("a"))
                    fout.write("\n")
                else:
                    self.sres("Writing {}\n\n".format(apargs.destinationfilename), asciigraphicscode=32)
                    fout = open(apargs.destinationfilename, ("w"))

                fout.write(cellcontents)
                fout.close()
            else:
                self.sres(ap_writefilepc.format_help())
            if not apargs.execute:
                return None
            return cellcontents  # should add in some blank lines at top to get errors right

        if percentcommand == "%mpy-cross":
            apargs = parseap(ap_mpycross, percentstringargs[1:])
            if apargs and apargs.set_exe:
                self.mpycrossexe = apargs.set_exe
            elif apargs.pyfile:
                if self.mpycrossexe:
                    self.dc.mpycross(self.mpycrossexe, apargs.pyfile)
                else:
                    self.sres("Cross compiler executable not yet set\n", 31)
                    self.sres(
                        "try: %mpy-cross --set-exe /home/julian/extrepositories/micropython/mpy-cross/mpy-cross\n")
                if self.mpycrossexe:
                    self.mpycrossexe = "/home/julian/extrepositories/micropython/mpy-cross/mpy-cross"
            else:
                self.sres(ap_mpycross.format_help())
            return cellcontents.strip() and cellcontents or None

        if percentcommand == "%comment":
            self.sres(" ".join(percentstringargs[1:]), asciigraphicscode=32)
            return cellcontents.strip() and cellcontents or None

        if percentcommand == "%lsmagic":
            self.sres(re.sub("usage: ", "", ap_capture.format_usage()))
            self.sres("    records output to a file\n\n")
            self.sres("%comment\n    print this into output\n\n")
            self.sres(re.sub("usage: ", "", ap_disconnect.format_usage()))
            self.sres("    disconnects from web/serial connection\n\n")
            self.sres(re.sub("usage: ", "", ap_esptool.format_usage()))
            self.sres("    commands for flashing your esp-device\n\n")
            self.sres(re.sub("usage: ", "", ap_fetchfile.format_usage()))
            self.sres("    fetch and save a file from the device\n\n")
            self.sres(re.sub("usage: ", "", ap_ls.format_usage()))
            self.sres("    list files on the device\n\n")
            self.sres("%lsmagic\n    list magic commands\n\n")
            self.sres(re.sub("usage: ", "", ap_mpycross.format_usage()))
            self.sres("    cross-compile a .py file to a .mpy file\n\n")
            self.sres(re.sub("usage: ", "", ap_readbytes.format_usage()))
            self.sres("    does serial.read_all()\n\n")
            self.sres("%rebootdevice\n    reboots device\n\n")
            self.sres(re.sub("usage: ", "", ap_sendtofile.format_usage()))
            self.sres("    send cell contents or file/direcectory to the device\n\n")
            self.sres(re.sub("usage: ", "", ap_serialconnect.format_usage()))
            self.sres("    connects to a device over USB wire\n\n")
            self.sres(re.sub("usage: ", "", ap_socketconnect.format_usage()))
            self.sres("    connects to a socket of a device over wifi\n\n")
            self.sres("%suppressendcode\n    doesn't send x04 or wait to read after sending the contents of the cell\n")
            self.sres("  (assists for debugging using %writebytes and %readbytes)\n\n")
            self.sres(re.sub("usage: ", "", ap_websocketconnect.format_usage()))
            self.sres("    connects to the webREPL websocket of an ESP8266 over wifi\n")
            self.sres("    websocketurl defaults to ws://192.168.4.1:8266 but be sure to be connected\n\n")
            self.sres(re.sub("usage: ", "", ap_writebytes.format_usage()))
            self.sres("    does serial.write() of the python quoted string given\n\n")
            self.sres(re.sub("usage: ", "", ap_writefilepc.format_usage()))
            self.sres("    write contents of cell to a file\n\n")

            return None

        if percentcommand == ap_disconnect.prog:
            apargs = parseap(ap_disconnect, percentstringargs[1:])
            self.dc.disconnect(raw=apargs.raw, verbose=True)
            return None

        # remaining commands require a connection
        if not self.dc.serialexists():
            return cellcontents

        if percentcommand == ap_plot.prog:
            apargs = parseap(ap_plot, percentstringargs[1:])
            if apargs.mode == 'matplotlib':
                self.sresplotmode = 1  # matplotlib-esque (array) plotting
            elif apargs.mode == 'live':
                self.sresplotmode = 2  # live plotting

            elif apargs.mode == 'livescroll':
                self.sresplotmode = 4  # live plotting with auto-scroll

            elif apargs.mode == 'scope':
                self.sresplotmode = 3  # scope-style
                self.sres_trigger_lvl = apargs.trigger_lvl
                self.sres_trig_RISE = True if apargs.type == 'RISE' else False
                self.sres_trig_chan = apargs.chan

            elif apargs.mode == 'none':
                self.sresplotmode = 0
            else:
                self.sresplotmode = DEFAULT_PLOT_MODE
            return cellcontents

        if percentcommand == ap_bypass.prog:
            self.bypass = True

        if percentcommand == ap_capture.prog:
            apargs = parseap(ap_capture, percentstringargs[1:])
            if apargs:
                self.sres("Writing output to file {}\n\n".format(apargs.outputfilename), asciigraphicscode=32)
                self.srescapturedoutputfile = open(apargs.outputfilename, "w")
                self.srescapturemode = (3 if apargs.QUIET else (2 if apargs.quiet else 1))
                self.srescapturedlinecount = 0
            else:
                self.sres(ap_capture.format_help())
            return cellcontents

        if percentcommand == ap_writebytes.prog:
            # (not effectively using the --binary setting)
            apargs = parseap(ap_writebytes, percentstringargs[1:])
            if apargs:
                bytestosend = apargs.stringtosend.encode().decode("unicode_escape").encode()
                res = self.dc.writebytes(bytestosend)
                if apargs.verbose:
                    self.sres(res, asciigraphicscode=34)
            else:
                self.sres(ap_writebytes.format_help())
            return cellcontents.strip() and cellcontents or None

        if percentcommand == ap_readbytes.prog:
            # (not effectively using the --binary setting)
            apargs = parseap(ap_readbytes, percentstringargs[1:])
            time.sleep(
                0.1)  # just give it a moment if running on from a series of values (could use an --expect keyword)
            l = self.dc.workingserialreadall()
            if apargs.binary:
                self.sres(repr(l))
            elif type(l) == bytes:
                self.sres(l.decode(errors="ignore"))
            else:
                self.sres(l)  # strings come back from webrepl
            return cellcontents.strip() and cellcontents or None

        if percentcommand == "%rebootdevice":
            self.dc.sendrebootmessage()
            self.dc.enterpastemode()
            return cellcontents.strip() and cellcontents or None

        if percentcommand == "%reboot":
            self.sres("Did you mean %rebootdevice?\n", 31)
            return None

        if percentcommand == "%%writetofile" or percentcommand == "%writefile":
            self.sres("Did you mean %%writefile?\n", 31)
            return None

        if percentcommand == "%serialdisconnect":
            self.sres("Did you mean %disconnect?\n", 31)
            return None

        if percentcommand == "%sendbytes":
            self.sres("Did you mean %writebytes?\n", 31)
            return None

        if percentcommand == "%reboot":
            self.sres("Did you mean %rebootdevice?\n", 31)
            return None

        if percentcommand in ("%savetofile", "%savefile", "%sendfile"):
            self.sres("Did you mean to write %sendtofile?\n", 31)
            return None

        if percentcommand in ("%readfile", "%fetchfromfile"):
            self.sres("Did you mean to write %fetchfile?\n", 31)
            return None

        if percentcommand == ap_fetchfile.prog:
            apargs = parseap(ap_fetchfile, percentstringargs[1:])
            if apargs:
                fetchedcontents = self.dc.fetchfile(apargs.sourcefilename, apargs.binary, apargs.quiet)
                if apargs.print:
                    self.sres(fetchedcontents.decode() if type(fetchedcontents) == bytes else fetchedcontents,
                              clear_output=True)

                if (apargs.destinationfilename or (not apargs.print and not apargs.load)) and fetchedcontents:
                    dstfile = apargs.destinationfilename or os.path.basename(apargs.sourcefilename)
                    self.sres("Saving file to {}".format(repr(dstfile)))
                    fout = open(dstfile, "wb" if apargs.binary else "w")
                    fout.write(fetchedcontents)
                    fout.close()

                if apargs.load:
                    fcontents = fetchedcontents.decode() if type(fetchedcontents) == bytes else fetchedcontents
                    if not apargs.quiet:
                        fcontents = "#%s\n\n%s" % (" ".join(percentstringargs), fcontents)
                    set_next_input_payload = {"source": "set_next_input", "text": fcontents, "replace": True}
                    return set_next_input_payload

            else:
                self.sres(ap_fetchfile.format_help())
            return None

        if percentcommand == ap_ls.prog:
            apargs = parseap(ap_ls, percentstringargs[1:])
            if apargs:
                self.dc.listdir(apargs.dirname or "", apargs.recurse)
            else:
                self.sres(ap_ls.format_help())
            return None

        if percentcommand == ap_sendtofile.prog:
            apargs = parseap(ap_sendtofile, percentstringargs[1:])
            if apargs and not (apargs.source == "<<cellcontents>>" and not apargs.destinationfilename) and (
                    apargs.source != None):

                destfn = apargs.destinationfilename

                def sendtofile(filename, contents):
                    self.dc.sendtofile(filename, apargs.mkdir, apargs.append, apargs.binary, apargs.quiet, contents)

                if apargs.source == "<<cellcontents>>":
                    filecontents = cellcontents
                    if not apargs.execute:
                        cellcontents = None
                    sendtofile(destfn, filecontents)

                else:
                    mode = "rb" if apargs.binary else "r"
                    if not destfn:
                        destfn = os.path.basename(apargs.source)
                    elif destfn[-1] == "/":
                        destfn += os.path.basename(apargs.source)

                    if os.path.isfile(apargs.source):
                        filecontents = open(apargs.source, mode).read()
                        if apargs.execute:
                            self.sres("Cannot excecute sourced file\n", 31)
                        sendtofile(destfn, filecontents)

                    elif os.path.isdir(apargs.source):
                        if apargs.execute:
                            self.sres("Cannot excecute folder\n", 31)
                        for root, dirs, files in os.walk(apargs.source):
                            for fn in files:
                                skip = False
                                fp = os.path.join(root, fn)
                                relpath = os.path.relpath(fp, apargs.source)
                                if relpath.endswith('.py'):
                                    # Check for compiled copy, skip py if exists
                                    if os.path.exists(fp[:-3] + '.mpy'):
                                        skip = True
                                if not skip:
                                    destpath = os.path.join(destfn, relpath).replace('\\', '/')
                                    filecontents = open(os.path.join(root, fn), mode).read()
                                    sendtofile(destpath, filecontents)
            else:
                self.sres(ap_sendtofile.format_help())
            return cellcontents  # allows for repeat %sendtofile in same cell

        self.sres("Unrecognized percentline {}\n".format([percentline]), 31)
        return cellcontents

    def runnormalcell(self, cellcontents, bsuppressendcode):
        cmdlines = cellcontents.splitlines(True)
        r = self.dc.workingserialreadall()
        if r:
            self.sres('[priorstuff] ')
            self.sres(str(r))

        for line in cmdlines:
            if line:
                if line[-2:] == '\r\n':
                    line = line[:-2]
                elif line[-1] == '\n':
                    line = line[:-1]
                self.dc.writeline(line)
                r = self.dc.workingserialreadall()
                if r:
                    self.sres('[duringwriting] ')
                    self.sres(str(r))

                    # if self.sresplotmode == 1:
                    #    self.sres('Plotting ON')
                    #    self.sresPLOT(str(r))

        if not bsuppressendcode:
            self.dc.writebytes(b'\r\x04')
            self.dc.receivestream(bseekokay=True, isplotting=self.sresplotmode)

    def sendcommand(self, cellcontents):
        bsuppressendcode = False  # can't yet see how to get this signal through

        if self.srescapturedoutputfile:
            self.srescapturedoutputfile.close()  # shouldn't normally get here
            self.sres("closing stuck open srescapturedoutputfile\n")
            self.srescapturedoutputfile = None

        # extract any %-commands we have here at the start (or ending?), tolerating pure comment lines and white space before the first % (if there's no %-command in there, then no lines at the front get dropped due to being comments)
        while True:
            mpercentline = re.match("(?:(?:\s*|(?:\s*#.*\n))*)(%.*)\n?(?:[ \r]*\n)?", cellcontents)
            if not mpercentline:
                break
            cellcontents = self.interpretpercentline(mpercentline.group(1), cellcontents[
                                                                            mpercentline.end():])  # discards the %command and a single blank line (if there is one) from the cell contents
            if isinstance(cellcontents, dict) and cellcontents.get("source") == "set_next_input":
                return cellcontents  # set_next_input_payload:
            if cellcontents is None:
                return None

        if not self.dc.serialexists():
            self.sres("No serial connected\n", 31)
            self.sres("  %serialconnect to connect\n")
            self.sres("  %esptool to flash the device\n")
            self.sres("  %lsmagic to list commands")
            return None

        # run the cell contents as normal
        if cellcontents:
            self.runnormalcell(cellcontents, bsuppressendcode)
        return None

    def sresSYS(self, output, clear_output=False):  # system call
        self.sres(output, asciigraphicscode=34, clear_output=clear_output)

    # 1=bold, 31=red, 32=green, 34=blue; from http://ascii-table.com/ansi-escape-sequences.php
    def sres(self, output, asciigraphicscode=None, n04count=0, clear_output=False):
        if self.silent:
            return

        if self.srescapturedoutputfile and (n04count == 0) and not asciigraphicscode:
            self.srescapturedoutputfile.write(output)
            self.srescapturedlinecount += len(output.split("\n")) - 1
            if self.srescapturemode == 3:  # 0 none, 1 print lines, 2 print on-going line count (--quiet), 3 print only final line count (--QUIET)
                return

            # changes the printing out to a lines captured statement every 1second.
            if self.srescapturemode == 2:  # (allow stderrors to drop through to normal printing
                srescapturedtime = time.time()
                if srescapturedtime < self.srescapturedlasttime + 1:  # update no more frequently than once a second
                    return
                self.srescapturedlasttime = srescapturedtime
                clear_output = True
                output = "{} lines captured".format(self.srescapturedlinecount)

        if clear_output:  # used when updating lines printed
            self.send_response(self.iopub_socket, 'clear_output', {"wait": True})
        if asciigraphicscode:
            output = "\x1b[{}m{}\x1b[0m".format(asciigraphicscode, output)

        stream_content = {'name': ("stdout" if n04count == 0 else "stderr"), 'text': output}

        self.send_response(self.iopub_socket, 'stream', stream_content)

    def sresPLOT(self, output: str, asciigraphicscode=None, n04count=0, clear_output=False):
        # logging.debug(output)
        if self.silent:
            return

        # if output is None or output == '':
        #    return

        if self.sresplotmode == 0:  # Plotting on but no plot commands used in code
            self.sres(output, n04count=n04count)
            return

        if self.sresplotmode == 1:  # matplotlib-esque (array) plotting

            if output == ATTRIBUTE_PREFIX + "--update" and self.sresstartedplot:
                self.sendPLOT()
                return None

            # ------------------- PLOT SETTINGS ---------------------
            # TODO: Make this more robust against LaTeX syntax containing {} in labels.
            if output != None and ATTRIBUTE_PREFIX in output:  # User is trying change the plot settings
                try:
                    if self.ax != None:  # If plot was made
                        output = output.replace(ATTRIBUTE_PREFIX, '')
                        ii = output.find('(')
                        jj = output.find('{') - 2 if '{' in output else output.find(')')
                        attribute = output[:ii]

                        filtered_attribute = VALID_ATTRIBUTES.get(attribute, attribute)

                        args = output[ii + 1:jj]

                        if args != '':
                            args = args.split(', ')
                            print(args)
                            for ii, arg in enumerate(args):
                                if sum(cc.isalpha() for cc in arg) == 0:  # Numbers
                                    args[ii] = float(arg)
                                else:
                                    args[ii] = arg.replace('"', '').replace('\'', '')
                        else:
                            args = ()

                        if '{' in output and '{}' not in output:  # read kwargs
                            kwargs = output[jj + 2:output.find(')')]
                            kwargs = ast.literal_eval(kwargs)
                        else:
                            kwargs = {}
                    else:
                        return None
                except (AttributeError, SyntaxError, ValueError) as e:
                    # Catch formatting errors
                    self.sres(output, n04count=n04count)
                    return None

                try:
                    getattr(self.ax, filtered_attribute)(*args, **kwargs)  # Run command
                    logging.debug(f'Plot setting {filtered_attribute} changed')
                    return None
                except Exception:
                    # Pass plotting errors to user
                    tb = traceback.format_exc()
                    self.sres(tb, n04count=1)
                    return None

            # ----------- PLOT DATA -------------------
            elif output != None and PLOT_PREFIX in output:

                try:  # Normal plot, no settings
                    output = output.replace(PLOT_PREFIX, '')
                    kk = output.rfind('}')
                    settings = output[:kk + 1]
                    data = output[kk + 1:]

                    settings = ast.literal_eval(settings)

                    ii = data.find('], [')
                    xx_hex_data, yy_hex_data = (data[2: ii], data[ii + 4:data.rfind(']') - 1])

                    xx_returned_data = bytearray(binascii.unhexlify(xx_hex_data))
                    yy_returned_data = bytearray(binascii.unhexlify(yy_hex_data))

                    try:
                        yy_shape_string = data[data.rfind('('):]
                        yy_shape = ast.literal_eval(yy_shape_string)
                    except Exception as e:
                        raise RuntimeError(f'Couldn\'t read shape from string: {yy_shape_string}') from e

                    self.xx = np.frombuffer(xx_returned_data, dtype=np.float32)
                    self.yy = np.frombuffer(yy_returned_data, dtype='f').reshape(yy_shape)

                except Exception as e:
                    # Incorrect formatting, this should not happen when using
                    # The plotting module for the ALPACA
                    self.sres(output, n04count=n04count)
                    logging.debug(traceback.format_exc())
                    return None

                # the data is good and plotting can commence
                if not self.sresstartedplot:
                    logging.debug('Created a new plot')
                    self.sresPLOTcreator()

                # default value for [fmt]
                fmt = settings.pop('fmt', '')
                kwargs = {}
                for key, value in settings.items():
                    if key in VALID_KEYS:
                        kwargs[key] = value

                try:
                    self.ax.plot(self.xx, self.yy, fmt, **kwargs)
                except Exception:
                    # Pass plotting errors to user
                    tb = traceback.format_exc()
                    self.sres(tb, n04count=1)
                    return None
                return None

            else:  # Not something to plot, just print
                self.sres(output, n04count=n04count)

        if self.sresplotmode in (2, 3, 4):  # Thonny-eqsue plotting or scope-esque plotting
            # format print("Random walk:", p1, " just random:", p2)
            try:

                if sum(cc.isnumeric() for cc in output) == 0:  # Plain text print statement
                    self.sres(output)
                    return

                data = unpack_Thonny_string(output)
            except ValueError:
                self.sres(output, n04count=n04count)
                return

            # the data is good and plotting can commence

            if not self.sresstartedplot:  # or len(data) != self.number_lines: # (re)Instantiation
                try:
                    # if not self.sresstartedplot:
                    self.sresPLOTcreator()
                    self.sresstartedplottime = time.time()

                    self.number_lines = len(data)

                    self.yy_minimum = 1_000
                    self.yy_maximum = -1_000

                    self.xx_minimum = 1_000
                    self.xx_maximum = -1_000

                    # Sanitize trigger channel input with knowledge of amount of data
                    if self.sres_trig_chan < 1:
                        self.sres_trig_chan = 1
                    elif self.sres_trig_chan > self.number_lines:
                        self.sres_trig_chan = self.number_lines

                    self.yy = np.zeros((0, self.number_lines))
                    self.xx = np.zeros(0)
                    self.lines = self.ax.plot(self.xx, self.yy)
                    for ii, line in enumerate(self.lines):
                        line.set_label(list(data.keys())[ii])

                    self.ax.legend(loc='upper center', ncol=3)
                    self.ax.grid()
                    self.ax.set_xlabel("Time [s]")
                except Exception as e:
                    self.sresstartedplot = 0
                    self.sres(output, n04count=n04count)
                    logging.exception(e)
                    return None

            try:
                if self.sresplotmode == 4: # EEG style (auto-scroll)
                    if self.sresliveiteration < 100:
                        self.yy = np.append(self.yy, [list(data.values())], axis=0)
                        self.xx = np.append(self.xx, time.time() - self.sresstartedplottime)
                    else:
                        self.yy = np.append(self.yy[1:, :], [list(data.values())], axis=0)
                        self.xx = np.append(self.xx[1:], time.time() - self.sresstartedplottime)

                elif self.sresplotmode == 2: # Live plot (no scroll)
                    self.yy = np.append(self.yy, [list(data.values())], axis=0)
                    self.xx = np.append(self.xx, time.time() - self.sresstartedplottime)

                else:
                    data_l = list(data.values())
                    value = data_l[self.sres_trig_chan - 1]
                    triggered_now = False
                    if self.sres_trig_RISE:
                        if not self.sres_trig_cldwn and value > self.sres_trigger_lvl:
                            triggered_now = True
                        if self.sres_trig_cldwn and value < self.sres_trigger_lvl:
                            self.sres_trig_cldwn = False
                    else:
                        if not self.sres_trig_cldwn and value < self.sres_trigger_lvl:
                            triggered_now = True
                        if self.sres_trig_cldwn and value > self.sres_trigger_lvl:
                            self.sres_trig_cldwn = False

                    if triggered_now:
                        self.sres_has_trig = True
                        self.sres_trig_cldwn = True
                        self.sresstartedplottime = time.time()
                        self.yy = np.zeros((0, self.number_lines))
                        self.xx = np.zeros(0)

                    self.yy = np.append(self.yy, [data_l], axis=0)
                    self.xx = np.append(self.xx, time.time() - self.sresstartedplottime)

                # self.ax.cla() # Clear
                for ii, line in enumerate(self.lines):
                    line.set_xdata(self.xx)
                    line.set_ydata(self.yy[:, ii])

                # self.ax.autoscale()
                yy_minimum = np.amin(self.yy)
                yy_maximum = np.amax(self.yy)
                xx_minimum = np.amin(self.xx)
                xx_maximum = np.amax(self.xx)
                if self.sresplotmode == 2:
                    yy_edge_size = (yy_maximum - yy_minimum) / 10
                    xx_edge_size = (xx_maximum - xx_minimum) / 10

                    self.ax.set_ylim(yy_minimum - yy_edge_size, yy_maximum + yy_edge_size * 2)  # Extra space for legend
                    self.ax.set_xlim(xx_minimum - xx_edge_size, xx_maximum + xx_edge_size)
                else:
                    if ((yy_minimum < self.yy_minimum)
                            or (yy_maximum > self.yy_maximum)
                            or (xx_minimum < self.xx_minimum)
                            or (xx_maximum > self.xx_maximum)):
                        self.yy_minimum = min(yy_minimum, self.yy_minimum)
                        self.yy_maximum = max(yy_maximum, self.yy_maximum)
                        self.xx_minimum = min(xx_minimum, self.xx_minimum)
                        self.xx_maximum = max(xx_maximum, self.xx_maximum)

                        yy_edge_size = (self.yy_maximum - self.yy_minimum) / 10
                        xx_edge_size = (self.xx_maximum - self.xx_minimum) / 10

                        self.ax.set_ylim(self.yy_minimum - yy_edge_size,
                                         self.yy_maximum + yy_edge_size * 2)  # Extra space for legend
                        self.ax.set_xlim(self.xx_minimum - xx_edge_size, self.xx_maximum + xx_edge_size)
                # self.ax.plot(self.xx, self.yy, label =  # Plot

                if self.sresliveiteration:
                    self.sendPLOT(update_id=self.plot_uuid)  # Use old plot and display
                else:
                    self.plot_uuid = self.sendPLOT()  # Create new plot and store UUID

                self.sresliveiteration += 1
            except Exception as e:
                self.sres(output, n04count=n04count)
                logging.exception(e)
                return None
            return None

    def sresPLOTcreator(self):
        # We create the plot with matplotlib.
        self.fig, self.ax = plt.subplots(1, 1, figsize=(6, 4), dpi=100)
        self.sresstartedplot = True

    def sresPLOTkiller(self):
        self.sresplotmode = DEFAULT_PLOT_MODE  # Reset plot
        self.sresstartedplot = False
        self.fig, self.ax = (None, None)
        self.sresliveiteration = 0

    def sendPLOT(self, update_id=None):
        if self.sresstartedplot == 0:
            return None

        # We create a PNG out of this plot.
        png = _to_png(self.fig)

        if update_id == None:
            plot_uuid = uuid.uuid1()  # When creating a new plot
            update = False
        else:
            plot_uuid = update_id
            update = True

        # We send the standard output to the
        # client.
        # self.send_response(
        #    self.iopub_socket,
        #    'stream', {
        #        'name': 'stdout',
        #        'data': ('Plotting {n} '
        #                'data points'). \
        #                format(n=self.xx.size)})

        # We prepare the response with our rich
        # data (the plot).
        content = {
            # 'source': 'kernel',

            # This dictionary may contain
            # different MIME representations of
            # the output.
            'data': {
                'image/png': png
            },

            # We can specify the image size
            # in the metadata field.
            'metadata': {
                'image/png': {
                    'width': 600,
                    'height': 400
                }
            },

            'transient': {
                'display_id': str(plot_uuid)
            }
        }

        # We send the display_data message with
        # the contents.
        if update:

            # self.send_response(self.iopub_socket,
            #    'clear_output', {'wait' : True})

            self.send_response(self.iopub_socket,
                               'update_display_data', content)

            # logging.debug(f'Updated display data')
        else:  # creating new plot
            self.send_response(self.iopub_socket,
                               'display_data', content)

            return plot_uuid  # Return new UUID for futre reference
            # logging.debug(f'Created new display data')

    def do_execute(self, code, silent, store_history=True, user_expressions=None, allow_stdin=False):
        self.silent = silent
        if not code.strip():
            return {'status': 'ok', 'execution_count': self.execution_count, 'payload': [], 'user_expressions': {}}

        interrupted = False

        # clear buffer out before executing any commands (except the readbytes one)
        if self.dc.serialexists() and not re.match("\s*%readbytes|\s*%disconnect|\s*%serialconnect|\s*websocketconnect",
                                                   code):
            priorbuffer = None
            try:
                priorbuffer = self.dc.workingserialreadall()
            except KeyboardInterrupt:
                interrupted = True
            except OSError as e:
                priorbuffer = []
                self.sres("\n\n***Connection broken [%s]\n" % str(e.strerror), 31)
                self.sres("You may need to reconnect")
                self.dc.disconnect(raw=True, verbose=True)

            except websocket.WebSocketConnectionClosedException as e:
                priorbuffer = []
                self.sres("\n\n***Websocket connection broken [%s]\n" % str(e.strerror), 31)
                self.sres("You may need to reconnect")
                self.dc.disconnect(raw=True, verbose=True)

            if priorbuffer:
                if type(priorbuffer) == bytes:
                    try:
                        priorbuffer = priorbuffer.decode()
                    except UnicodeDecodeError:
                        priorbuffer = str(priorbuffer)

                for pbline in priorbuffer.splitlines():
                    if deviceconnector.wifimessageignore.match(pbline):
                        continue  # filter out boring wifi status messages
                    if pbline:
                        self.sres('[leftinbuffer] ')
                        self.sres(str([pbline]))
                        self.sres('\n')

        set_next_input_payload = None
        try:
            if not interrupted:
                set_next_input_payload = self.sendcommand(code)
        except KeyboardInterrupt:
            interrupted = True
        except OSError as e:
            self.sres("\n\n***OSError [%s]\n\n" % str(e.strerror))
        # except pexpect.EOF:
        #    self.sres(self.asyncmodule.before + 'Restarting Bash')
        #    self.startasyncmodule()

        if self.sresplotmode == 1:  # matplotlib-eqsue plotting (after finishing cell)
            self.sendPLOT()
        self.sresPLOTkiller()

        if self.srescapturedoutputfile:
            if self.srescapturemode == 2:
                self.send_response(self.iopub_socket, 'clear_output', {"wait": True})
            if self.srescapturemode == 2 or self.srescapturemode == 3:
                output = "{} lines captured.".format(
                    self.srescapturedlinecount)  # finish off by updating with the correct number captured
                stream_content = {'name': "stdout", 'text': output}
                self.send_response(self.iopub_socket, 'stream', stream_content)

            self.srescapturedoutputfile.close()
            self.srescapturedoutputfile = None
            self.srescapturemode = 0

        if interrupted:
            self.sresSYS("\n\n*** Sending Ctrl-C\n\n")
            if self.dc.serialexists():
                self.dc.writebytes(b'\r\x03')
                interrupted = True
                try:
                    self.dc.receivestream(bseekokay=False, b5secondtimeout=True)
                except KeyboardInterrupt:
                    self.sres("\n\nKeyboard interrupt while waiting response on Ctrl-C\n\n")
                except OSError as e:
                    self.sres("\n\n***OSError while issuing a Ctrl-C [%s]\n\n" % str(e.strerror))
            return {'status': 'abort', 'execution_count': self.execution_count}

        # everything already gone out with send_response(), but could detect errors (text between the two \x04s

        payload = [
            set_next_input_payload] if set_next_input_payload else []  # {"source": "set_next_input", "text": "some cell content", "replace": False}
        return {'status': 'ok', 'execution_count': self.execution_count, 'payload': payload, 'user_expressions': {}}
