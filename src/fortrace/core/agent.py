# Copyright (C) 2013-2014 Reinhard Stampp
# Copyright (C) 2017 Sascha Kopp
# This file is part of fortrace - http://fortrace.fbi.h-da.de
# See the file 'docs/LICENSE' for copying permission.

from __future__ import absolute_import

import shlex
import sys
import socket
import time
import logging
import base64
import subprocess
import platform
import datetime
import io
import os
import zipfile
import fortrace.utility.guesttime as gt
from threading import Lock, Thread, Condition

import shutil
import psutil

# TODO Remote Shell Exec, setOStime and Copy Directory to guest still having some issues, most functions work as intended


#from smbclient import (
#    shutil
#)

if platform.system() == "Windows":
    import win32api
    import pywintypes
    import six.moves.cPickle
    from fortrace.utility.winmessagepipe import WinMessagePipe

try:
    from fortrace.utility.logger_helper import create_logger
    from fortrace.utility.network import NetworkInfo
except ImportError as ie:
    raise Exception("agent " + str(ie))


class Agent(object):
    """
    fortrace agent, it runs inside guest; i.e. Windows 7 or Linux:
    """

    def __init__(self, operating_system="windows", logger=None):
        try:
            self._send_lock = Lock()
            self._pexec_threads = dict()
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.last_driven_url = ""
            self.window_is_crushed = False
            self.disconnectedByHost = False
            self.operatingSystem = operating_system
            self.applicationWindow = {}

            self.logger = logger
            if self.logger is None:
                self.logger = create_logger('agent', logging.DEBUG)

            # - TODO: add support for linux
            if operating_system == "Windows":
                from fortrace.inputDevice.inputDevice import InputDeviceManagement
                self.inputDeviceManager = InputDeviceManagement(self, logger)

            # open pipe for Windows admin commands
            if platform.system() == "Windows":
                self.adminpipe = WinMessagePipe()
                self.adminpipe.open("fortraceadmin", mode='w')

            self.logger.debug("agent::init finished")
        except Exception as e:
            raise Exception("Agent::init error: " + str(e))

    def connect(self, host, port):
        """
        After creation of this object, this method will connect to the vmm
        """
        while 1:
            try:
                self.logger.debug("try to connect...")
                self.sock.connect((host, port))
                if self.sock != 0:
                    break
                self.logger.debug("connected")
            except:
                time.sleep(1)
                self.logger.error("Error: can't connect to host " + host + ":" + str(port))

    def register(self):
        ip_local = NetworkInfo.get_local_IP()
        ip_internet = NetworkInfo.get_internet_IP()
        mac = NetworkInfo.get_MAC()
        internet_iface = NetworkInfo.get_internet_interface()
        local_iface = NetworkInfo.get_local_interface()
        self.send("register " + str(ip_internet) + " " + str(ip_local) + " " + str(
            mac) + " " + internet_iface + " " + local_iface)

    def do_command(self, command):
        """
        Check for keywords in 'command'

        Keywords:
          application <string to parse>

          inputDevice <string to parse>

          destroyConnection
        """
        try:
            self.logger.info("Agent::do_command")
            self.logger.debug("command: " + command)
            com = command.split(" ")

            package = com[0]

            if "application" in package:
                module = com[1]
                window_id = com[2]
                if len(com) > 4:
                    args = " ".join(com[4:])

                self.logger.debug("before loading module")
                # load class moduleGuestSide and moduleGuestSideCommands
                name = "fortrace." + package + "." + module
                self.logger.debug("module to load: " + name)
                mod = __import__(name, fromlist=[''])
                self.logger.debug("module '" + module + "' will be loaded via __import__")
                class_commands = getattr(mod, module[0].upper() + module[1:] + 'GuestSideCommands')
                self.logger.debug("module '" + module + "' is loaded via __import__")
                if module not in list(self.applicationWindow.keys()):
                    self.logger.debug("module '" + module + "' not in applicationWindow -> do add")
                    self.applicationWindow[module] = []

                window_exists = False
                for app_obj in self.applicationWindow[module]:
                    # call it's method
                    if app_obj.window_id is window_id:
                        self.logger.debug("module '" + module + "' with window " + str(window_id) + " exists")
                        window_exists = True
                        class_commands.commands(self, app_obj, " ".join(com[1:]))
                        self.logger.debug("mod_commands.commands are called")

                if not window_exists:
                    # create a new object
                    self.logger.debug("no window exists")
                    self.logger.debug("to create one, load module " + module + "GuestSide")
                    mod = __import__(name, fromlist=[''])
                    class_guest_side = getattr(mod, module[0].upper() + module[1:] + 'GuestSide')

                    self.logger.debug("create an instance")
                    app_obj = class_guest_side(self, self.logger)  # (agent_object, logger)
                    self.logger.debug("set window_id " + str(window_id))
                    app_obj.window_id = window_id
                    # process command string and call appropriate method
                    self.logger.debug("mod_commands.call commands")
                    class_commands.commands(self, app_obj, " ".join(com[1:]))
                    self.logger.debug("mod_commands.commands are called")
                    self.applicationWindow[module].append(app_obj)
                    self.logger.debug("object is appended to the applicationWindow[module] list")

            elif "inputDevice" in package:
                """call the execution method from the inputDevice manager"""
                if len(com) < 2:
                    self.logger.error("inputDevice need one parameter: " + str(len(com) - 1) + " given")
                    return

                if self.operatingSystem != "Windows":
                    raise NotImplementedError("InputDeviceManagement is only implemented for windows by now")

                self.inputDeviceManager.execute(" ".join(com[1:]))

            elif "shellExec" in package:
                """decode and execute a command in the system shell"""
                cv = Condition()
                registered = [False]
                shell_exec_id = int(com[1])
                cmd = base64.b64decode(com[2])
                #cmd = com[2]
                print(cmd)
                path_prefix = base64.b64decode(com[3])
                #path_prefix = com[3]
                print(path_prefix)
                if len(com) == 5:
                    std_in = base64.b64decode(com[4])
                    #std_in = com[4]
                    print(std_in)
                else:
                    std_in = ""
                t = Thread(target=self._do_shell_exec,
                           kwargs={"shell_exec_id": shell_exec_id, "cmd": cmd, "path_prefix": path_prefix,
                                   "std_in": std_in, "condition": cv, "registered": registered})
                self._pexec_threads[shell_exec_id] = (t, None)  # save thread handles
                t.start()
                while not registered[0]:
                    with cv:
                        cv.wait()

            elif "remoteShellExec" in package:
                """copies file to vm and executes it in the system shell"""
                cv = Condition()
                registered = [False]
                shell_exec_id = int(com[1])
                filename = base64.b64decode(com[2])
                file = base64.b64decode(com[3])
                target_dir = base64.b64decode(com[4])
                path_prefix = target_dir
                if target_dir == "#unset":
                    target_dir = ""
                if len(com) == 6:
                    #std_in = base64.b64decode(com[5])
                    std_in = com[5]
                else:
                    std_in = ""
                try:
                    with open(target_dir + filename, 'wb') as f:
                        f.write(file)
                except IOError:
                    self.logger.error("File not writable: " + filename)
                t = Thread(target=self._do_shell_exec,
                           kwargs={"shell_exec_id": shell_exec_id, "cmd": filename, "path_prefix": path_prefix,
                                   "std_in": std_in, "condition": cv, "registered": registered})
                self._pexec_threads[shell_exec_id] = (t, None)  # save thread handles
                t.start()
                while not registered[0]:
                    with cv:
                        cv.wait()

            elif "killShellExec" in package:
                """kill previously started process via intern handle id"""
                handle = None
                ishell_exec_id = int(com[1])
                self.logger.debug("Trying to kill process with internal id: " + com[1])
                try:
                    handle = self._pexec_threads[ishell_exec_id][1]  # type: subprocess.Popen
                    if handle is not None:
                        # handle.kill()
                        p = psutil.Process(pid=handle.pid)
                        cs = p.children(recursive=True)
                        for c in cs:
                            c.kill()
                        p.kill()
                    else:
                        self.logger.critical(
                            "This process id has no handle. This should not happen! [" + str(ishell_exec_id) + "]")
                except KeyError:
                    self.logger.error("The specified handle id was not found: " + str(ishell_exec_id))
                except:
                    self.logger.error(
                        "Failed to kill process with handle_id/pid: " + str(ishell_exec_id) + "/" + handle.pid)

            elif "file" in package:
                fcmd = com[1]
                if "filecopy" in fcmd:
                    tname = base64.b64decode(com[2])
                    print(tname)
                    #tname = com[2]
                    #file = com[3]
                    padding = "===".encode()
                    print(type(com[3]))
                    tmpfile = com[3].encode()
                    file = base64.b64decode(tmpfile + padding)
                    self._filecopy(tname, file)
                elif "dircopy" in fcmd:
                    tdir = base64.b64decode(com[2])
                    zfile = base64.b64decode(com[3])
                    #tdir = com[2]
                    #zfile = com[3]
                    print(type(tdir))
                    print(type(zfile))
                    self._dircopy(tdir, zfile)
                elif "dircreate" in fcmd:
                    tdir = base64.b64decode(com[2])
                    #tdir = com[2]
                    self._dircreate(tdir)
                elif "touch" in fcmd:
                    tfile = base64.b64decode(com[2])
                    #tfile = com[2]
                    self._touchfile(tfile)
                elif "guestcopy" in fcmd:
                    sfile = base64.b64decode(com[2])
                    tfile = base64.b64decode(com[3])
                    #sfile = com[2]
                    #tfile = com[3]
                    self._guestcopy(sfile, tfile)
                elif "guestmove" in fcmd:
                    sfile = base64.b64decode(com[2])
                    tfile = base64.b64decode(com[3])
                    #sfile = com[2]
                    #tfile = com[3]
                    self._guestmove(sfile, tfile)
                elif "guestdelete" in fcmd:
                    tpath = base64.b64decode(com[2])
                    #tpath = com[2]
                    self._guestdelete(tpath)
                elif "guestchdir" in fcmd:
                    cpath = base64.b64decode(com[2])
                    #cpath = com[2]
                    self._guestchdir(cpath)

             #   elif "smbcopy" in fcmd:
            #        sfile = base64.b64decode(com[2])
           #         tfile = base64.b64decode(com[3])
          #          user = base64.b64decode(com[4])
         #           passwd = base64.b64decode(com[5])
        #            self._smbcopy(sfile, tfile, user, passwd)
                else:
                    self.logger.warning("Unrecognized file command: " + fcmd)

            elif "setOSTime" in package:
                """set the OSes time"""
                ptime = base64.b64decode(com[1])
                #ptime = com[1]
                local_time = com[2]
                if local_time == "True":
                    blocal_time = True
                else:
                    blocal_time = False
                try:
                    msg = {"cmd": "setostime", "param": [ptime, local_time]}
                    msg = six.moves.cPickle.dumps(msg)
                    self.adminpipe.write(msg)
                except six.moves.cPickle.PickleError:
                    self.logger.error("Cannot pickle command data!")
                except OSError:
                    self.logger.warning("Sending command to supplementary Agent failed, using fallback!")
                    self._set_os_time(ptime, blocal_time)

            elif "guesttime" in package:
                """
                return guesttime
                """
                self._guesttime()

            elif "guesttzone" in package:
                """
                return guest timezone
                """
                self._guesttimezone()

            elif "runElevated" in package:
                """
                added by Thomas Schaefer in 2019, running a shell command with admin rights
                """
                import os
                self.logger.debug("agent runElevated")
                command = base64.b64decode(com[1])
                #command = com[1]
                try:
                    self.logger.debug("msg: " + command)
                    p = r'''Powershell -Command "& { Start-Process ''' + command.replace('"', '\\"') + ''' -Verb RunAs } " '''
                    self.logger.debug("command: " + p)
                    os.system(p)
                except cPickle.PickleError:
                    self.logger.warning("Cannot pickle command data!")
                except OSError:
                    self.logger.warning("Sending command to supplementary Agent failed!")

            elif "cleanUp" in package:
                """
                added by Thomas Schaefer in 2019, reducing artefacts left by fortrace
                expanded by Stephan Maltan in 2021 further reducing the left artifacts by removing the fortrace-directory from all existing users Desktop
                """
                self.logger.debug("agent cleanUp")
                command = base64.b64decode(com[1])
                try:
                    import subprocess
                    import os
                    # cleaning registry entries
                    if(platform.system() == "Windows"):
                        os.system('reg delete \"HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\ComDlg32\\OpenSavePidlMRU\\py\"  /f')
                        os.system('reg delete \"HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\ComDlg32\\OpenSavePidlMRU\\pyc\"  /f')
                    # cleaning filesystem
                    # os.system("rmdir /s /q C:\\Users\\Bill\\Desktop\\fortrace")
                    os.system("rmdir /s /q C:\\Python27\\Lib\\site-packages\\fortrace")
                except OSError:
                    self.logger.warning("Executing commands failed.")

            elif "destroyConnection" in com[0]:
                self.disconnectedByHost = True
                self.destroyConnection()

            else:
                self.logger.error("command " + com[0] + " not found!")
        except Exception as e:
            self.logger.error(str(e))

    def _do_shell_exec(self, shell_exec_id, cmd, path_prefix, std_in, condition, registered):
        """ Actually run shellExec.

        :type registered: list
        :type condition: Condition
        :param registered: Condition for condition variable
        :param condition: A condition variable to notify setting of handle
        :param shell_exec_id: id of this call
        :param cmd: the command to execute
        :param path_prefix: a path that should be prefixed to cmd
        :param std_in: text input for interactive console programs
        """
        # wtf is this stuff doing here
        # if platform.system() == "Windows":
        #    subprocess.call(["taskkill", "/IM", "firefox.exe", "/F"])
        # elif platform.system() == "Linux":
        #    os.system("pkill firefox")
        # else:
        #    raise NotImplemented("Not implemented for system: " + platform.system())
        path_prefix = path_prefix.decode()
        cmd = cmd.decode()
        if path_prefix != "#unset":
            if sys.platform == "win32":
                cmd = path_prefix + "\\" + cmd
            else:
                cmd = path_prefix + "/" + cmd
        print(cmd)
        try:
            p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.PIPE)
            #p.wait()
            print(cmd)
            self._pexec_threads[shell_exec_id] = (self._pexec_threads[shell_exec_id][0], p)  # save handle to allow kill
        except:
            #pass
            self.logger.info("No process started")
        finally:
            registered[0] = True
            with condition:
                condition.notifyAll()
                std_in = std_in.encode()
        std_out, std_err = p.communicate(input=std_in)
        std_out = std_out.decode()
        std_err = std_err.decode()
        exit_code = p.returncode
        #std_out = base64.b64encode(std_out)  # base64 encode to avoid fragmentation
        #std_err = base64.b64encode(std_err)  # base64 encode to avoid fragmentation
        #self.send("shellExecComplete " + str(shell_exec_id) + " " + str(exit_code) + " " + std_out + " " + std_err)
        self.send("shellExecComplete " + str(shell_exec_id) + " " + str(exit_code) + " " + std_out + " " + std_err)

    def _set_os_time(self, ptime, local_time=True):
        """
        Sets the systems time to the specifies date
            This may need admin rights on Windows

            :type local_time: bool
            :type ptime: str
            :param ptime: a posix date string in format "%Y-%m-%d %H:%M:%S"
            :param local_time: is this local time
        """
        ptime = ptime.decode()
        try:
            t = time.strptime(ptime, "%Y-%m-%d %H:%M:%S")
            self.logger.info("Trying to set time to: " + ptime)
        except ValueError:
            self.logger.error("Bad datetime format")
            return
        if platform.system() == "Windows":
            try:
                if local_time:
                    wt = pywintypes.Time(t)
                    rval = win32api.SetLocalTime(wt)  # you may prefer localtime
                else:
                    rval = win32api.SetSystemTime(t[0], t[1], t[6], t[2], t[3], t[4], t[5], 0)
                if rval == 0:
                    self.logger.error("Setting system time failed - function returned 0 - error code is {0}".format(
                        str(win32api.GetLastError())))
            except win32api.error:
                self.logger.error("Setting system time failed due to exception!")
        elif platform.system() == "Linux":
            pass  # todo: implement
        else:
            pass  # everything else unsupported

    def destroyConnection(self):
        """
        Close the socket

        Will close the open socket to the  and end the
        """
        try:
            self.sock.close()
            self.logger.info("interactionmanager end!")
            sys.exit(0)
        except Exception as e:
            logging.error("destroyConnection - Error:" + str(e))

    def send(self, msg):
        """Forward messages to the vmm"""
        self._send_lock.acquire()  # prevent parallel sending from multiple threads
        message_size = "%.8x" % len(msg)
        buffer = message_size + msg
        self.logger.debug("sent: " + buffer)
        sent = self.sock.send(buffer.encode())
        if sent == 0:
            self._send_lock.release()
            raise RuntimeError("socket connection broken")
        else:
            self._send_lock.release()

    def receiveCommands(self):
        """receive commands from the vmm in an infinite loop"""
        msg = ""
        allreceived = False
        try:
            # try long as there are unfinished received commands
            while 1:
                if allreceived:
                    break
                if self.disconnectedByHost:
                    break
                chunk = self.sock.recv(1024).decode()
                if chunk == '':
                    raise RuntimeError("socket connection broken")
                # get length of the message
                # self.logger.error("complete chunk: " + chunk)
                #chunk = chunk.decode()
                msg = msg + chunk
                # if msg do not contain the message length
                if len(msg) < 8:
                    self.logger.error("half command")
                    continue

                message_size = int(msg[0:8], 16)

                if len(msg) < (message_size + 8):
                    continue

                # get the commands out of the message
                while len(msg) >= (message_size + 8):
                    # if the command fit into message, return the command list commands
                    if len(msg) == message_size + 8:
                        self.do_command(msg[8:(message_size + 8)])
                        msg = ""
                        allreceived = True
                        break
                    # there are multiple commands in the message
                    else:
                        if len(msg) < (message_size + 8):
                            continue

                        command = msg[8:(message_size + 8)]
                        msg = msg[(message_size + 8):]

                        message_size = int(msg[0:8], 16)
                        self.do_command(command)
        except Exception as e:
            self.logger.error("error recv: " + str(e))

        return

    def _filecopy(self, target_name, file_content):
        """ Writes a file to disk.

        :param target_name: filename to write to
        :param file_content: content of file
        """
        self.logger.debug("File receive to: " + str(target_name))
        with open(target_name, 'wb') as f:
            f.write(file_content)

    def _dircopy(self, target_directory, zip_file):
        """ Writes a directory to disk.

        :param target_directory: unpack path
        :param zip_file: string containing a zip-file
        """
        self.logger.debug("Directory receive to: " + str(target_directory))
        zbuf = io.BytesIO()
        print(type(zbuf))
        zbuf.write(zip_file)
        z = zipfile.ZipFile(zbuf)
        z.extractall(target_directory)
        z.close()
        zbuf.close()

    def _dircreate(self, target_directory):
        """ Creates a directory.

        :param target_directory: directory path to create
        """
        self.logger.debug("Create directoty at: " + str(target_directory))
        os.mkdir(target_directory)

    def _touchfile(self, target_path):
        """ Touches a file.

        :param target_path: file to touch
        """
        self.logger.debug("Touching file at: " + str(target_path))
        with open(target_path, 'a'):
            os.utime(target_path, None)

    def _guestcopy(self, source_file, target_file):
        """ Copies file.

        :param source_file: source file
        :param target_file: target file
        """
        self.logger.debug("Copying file from: " + str(source_file) + " To: " + str(target_file))
        shutil.copy(source_file, target_file)

    def _guestmove(self, source_file, target_file):
        """ Moves file.

        :param source_file: source file
        :param target_file: target file
        """
        self.logger.debug("Moving file from: " + str(source_file) + " To: " + str(target_file))
        shutil.move(source_file, target_file)

    def _guestdelete(self, target_path):
        """ Delete file or directory on guest.

        :param target_path: file or directory to delete
        """
        self.logger.debug("Deleting file at: " + str(target_path))
        if os.path.isdir(target_path):
            shutil.rmtree(target_path, True)
        else:
            os.remove(target_path)

    #def _smbcopy(self, source_file, target_file, user, passwd):
      #  """ Copies file to smbshare.
        #   :param source_file: source file
          # :param target_file: target file
         #  :param user: SMB User
          # :param passwd: SMB Password
       #"""

        #shutil.copy(source_file, target_file, True, username=user, password=passwd)

    def _guestchdir(self, new_path):
        """ Changes the current working directory.

        :param new_path: New current working directory
        """
        os.chdir(new_path)

    def _guesttime(self):
        self.logger.debug("getting guest time")
        gTime = gt.getGuestTime()
        self.logger.debug("guest time is: {0}".format(gTime))
        self.agent_object.send("time {0}".format(gTime))

    def _guesttimezone(self):
        self.logger.debug("getting guest time")
        tzone = gt.getGuestTimezone()
        self.logger.debug("current timezone is: {0}".format(tzone))
        tzone64 = base64.b64encode(tzone)
        self.send("tzone {0}".format(tzone64))

    def _cleanUp(self, mode):
        """
        Cleanup function to wipe artifacts created by the framework during the execution.
        """
        import subprocess
        import os
        import getpass
        if (platform.system() == "Windows"):
            sysdrive = os.getenv("SystemDrive")
            active_user = getpass.getuser()
            sdelete_p = "{0}\\Users\\{1}\\Desktop\\fortrace\\contrib\\windows-utils\\sdelete64.exe".format(sysdrive, active_user)
            try:
                # Original Part of the cleanUp function, by Thomas Schäfer
                # cleaning Registry entries for Python (Windows 7 only?)
                os.system(
                        'reg delete \"HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\ComDlg32\\OpenSavePidlMRU\\py\"  /f')
                os.system(
                        'reg delete \"HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\ComDlg32\\OpenSavePidlMRU\\pyc\"  /f')
                # remove Python site packages (Windows 7 only?)
                os.system("rmdir /s /q C:\\Python27\\Lib\\site-packages\\fortrace")
                # Newly implemented  elements
                # clean automatic user login Registry entries
                logon_path = "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon"
                subprocess.call(["reg", "DELETE", logon_path, "/v", "AutoAdminLogon", "/f"])
                subprocess.call(["reg", "DELETE", logon_path, "/v", "DefaultUserName", "/f"])
                subprocess.call(["reg", "DELETE", logon_path, "/v", "DefaultPassword", "/f"])
            except OSError:
                self.logger.error("Executing commands failed.")
            # Get user list
            try:
                self.logger.debug("Retrieving List of active user accounts")
                user_list_op = str(subprocess.check_output(["wmic", "useraccount", "get", "name"]))
                user_list_op = user_list_op.replace("\\r", "").replace("\\n", "").replace("b'", "").replace("'", "").split()[1:-1]
                user_list = []
                for i in user_list_op:
                    if i != "" and i != "Administrator" and i != "DefaultAccount" and i != "Guest" and i != active_user:
                        user_list.append(i)
            except Exception as e:
                # when creating the user list fails return to scenario
                self.logger.error("An exception occured while getting the user list: " + e)
            # clean user specific artifacts
            self.logger.debug("Clean userspecific artifacts")
            # delete for each user the fortrace folder on the Desktop, the autostart entry and the python site packages
            for i in user_list:
                try:
                    user = i
                    self.logger.debug("delete fortrace folder of user {0}".format(user))
                    fortrace_dir = "{0}\\Users\\{1}\\Desktop\\fortrace".format(sysdrive, user)
                    subprocess.call([sdelete_p, "-s", "-r", "-q", "-p", "1", "-nobanner", "-accepteula", fortrace_dir])
                    self.logger.debug("delete fortrace autostart link of user {0}".format(user))
                    user_fortrace_autostart = "{0}\\Users\\{1}\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\startGuestAgent.lnk".format(sysdrive, user)
                    subprocess.call(["CMD", "/c", "del", user_fortrace_autostart, "/f", "/q"])
                    self.logger.debug("delete python folder of user {0}".format(user))
                    user_fortrace_python = "{0}\\Users\\{1}\\AppData\\Roaming\\Python\\Python27\\site-packages".format(
                        sysdrive, user)
                    subprocess.call(
                        [sdelete_p, "-s", "-r", "-q", "-p", "1", "-nobanner", "-accepteula", user_fortrace_python])
                except Exception as e:
                    self.logger.error("An exception occured while deleting the user specific artifacts: " + e)
            self.logger.debug("clear Prefetch")
            prefetch_choco = "{0}\\Windows\\Prefetch\\CHOCO*".format(sysdrive)
            prefetch_python = "{0}\\Windows\\Prefetch\\PYTHON*".format(sysdrive)
            prefetch_psexec = "{0}\\Windows\\Prefetch\\PSEXEC*".format(sysdrive)
            prefetch_pip = "{0}\\Windows\\Prefetch\\PIP*".format(sysdrive)
            try:
                # Delete Prefetch files connected to the framework
                self.logger.debug("clear Prefetch: " + prefetch_choco)
                subprocess.call([sdelete_p, "-s", "-r", "-q", "-p", "1", "-nobanner", "-accepteula", prefetch_choco])
                self.logger.debug("clear Prefetch: " + prefetch_python)
                subprocess.call([sdelete_p, "-s", "-r", "-q", "-p", "1", "-nobanner", "-accepteula", prefetch_python])
                self.logger.debug("clear Prefetch: " + prefetch_psexec)
                subprocess.call([sdelete_p, "-s", "-r", "-q", "-p", "1", "-nobanner", "-accepteula", prefetch_psexec])
                self.logger.debug("clear Prefetch: " + prefetch_pip)
                subprocess.call([sdelete_p, "-s", "-r", "-q", "-p", "1", "-nobanner", "-accepteula", prefetch_pip])
            except Exception as e:
                self.logger.error("An exception occured during the deletion of the prefetch files: " +e)
            self.logger.info("Cleanup finished.")
            if mode == "manual":
                self.logger.info("guest agent cleanup finished, window can be closed now")
            elif mode == "auto":
                self.logger.info("guest agent cleanup finished, shutting system down")
                try:
                    subprocess.call(["shutdown", "s", "/t", "10"])
                except Exception as e:
                    self.logger.error("system was not able to shut down: {0}".format(e))
            else:
                self.logger.error("unknown cleanup mode {0}".format(mode))
        else:
            self.logger.error("Unknown System Platform, only Windows is supported at the moment")

    def _initClean(self):
        """
        CleanUp function to wipe artifacts, which were created during the configuration of the template.
        """
        import subprocess
        import os
        import getpass
        if (platform.system() == "Windows"):
            self.logger.debug("Clearing Event Log")
            # Clear Event Log
            try:
                cmd = r"for /f %x in ('wevtutil enum-logs') do wevtutil clear-log %x"
                subprocess.call(["CMD", "/c", cmd])
            except Exception as e:
                self.logger.error("Clearing the Event Log failed")
            # Clear Prefetch
            self.logger.debug("clearing Prefetch data")
            sysdrive = os.getenv("SystemDrive")
            user = getpass.getuser()
            sdelete_p = "{0}\\Users\\{1}\\Desktop\\fortrace\\contrib\\windows-utils\\sdelete64.exe".format(sysdrive, user)
            prefetch_p = "{0}\\Windows\\Prefetch".format(sysdrive)
            try:
                subprocess.call([sdelete_p, "-s", "-r", "-q", "-p", "1", "-nobanner", "-accepteula", prefetch_p])
            except Exception as e:
                self.logger.debug("Clearing the  Prefetch files failed")
        else:
            self.logger.error("Unknown System Platform, only Windows is supported at the moment")
