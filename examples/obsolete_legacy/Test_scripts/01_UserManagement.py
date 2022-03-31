import time
import logging
import hashlib
import subprocess
import random
import os
from datetime import date

try:
    from fortrace.core.vmm import Vmm
    from fortrace.utility.logger_helper import create_logger
    from fortrace.core.vmm import GuestListener
    from fortrace.core.reporter import Reporter
    import fortrace.utility.scenarioHelper as scenH
except ImportError as ie:
    print("Import error! in fileManagement.py " + str(ie))
    exit(1)

#############################################
#               Values to define            #
#############################################
export_dir = "/data/export/"
vm_name = "Scenario{0}".format(random.randint(0, 9999))
author = "Stephan Maltan"
creation_date = date.today().strftime('%Y-%m-%d')


#############################################
#              Helper Functions             #
#############################################
# Do not make changes in this function!
def generate_file_sh256(filename, blocksize=2 ** 20):
    """
    Generates the SHA_256 hashsum of the given file
    @param filename: name of the file, the hashsum will calculated for
    @param blocksize: blocksize used during calculation
    """
    m = hashlib.sha256()
    with open(filename, "rb") as f:
        while True:
            buf = f.read(blocksize)
            if not buf:
                break
            m.update(buf)
    return m.hexdigest()

def wait(min=10, max=60):
    """
    Waits for a random amount of seconds in the provided interval
    Gives the user time to "think"
    @param min: minimum time to wait
    @param max: maximum time to wait
    """
    if min >= max:
        max = min + 30
    sleeptime = random.randint(min, max)
    time.sleep(sleeptime)


#############################################
#               Initialization              #
#############################################
hostplatform = "windows"
macsInUse = []
guests = []

logger = create_logger('fortraceManager', logging.INFO)
guestListener = GuestListener(guests, logger)
virtual_machine_monitor1 = Vmm(macsInUse, guests, logger)
guest = virtual_machine_monitor1.create_guest(guest_name=vm_name, platform="windows")
sc = scenH.Scenario(logger, Reporter(), guest)

sc.Reporter.add("imagename", vm_name)
sc.Reporter.add("author", author)
sc.Reporter.add("baseimage", hostplatform + "-template.qcow2")
#############################################
#                Scenario                   #
#############################################
####### Session 1 #######
# Wait for the VM to connect to the VMM
guest.waitTillAgentIsConnected()
# Create a bunch of new users and change to one of them on next reboot
sc.addUser("Fred", "1NOGK2X")
sc.addUser("George", "1NOGK2X")
sc.addUser("Bill", "1NOGK2X")
sc.addUser("Percy", "1NOGK2X")
sc.addUser("Charly", "1NOGK2X")
sc.addUser("Ginny", "1NOGK2X")
sc.changeUser("George", "1NOGK2X")
sc.reboot()
# Delete fortrace-user
sc.deleteUser("fortrace", "secure")
sc.deleteUser("Fred", "keep")
sc.deleteUser("Percy", "delete")
wait(300,500)
# try to delete itself
sc.deleteUser("George", "delete")
sc.changeUser("Ginny", "1NOGK2X")
sc.reboot()
# shut down system
sc.shutdown()
# Scenario finished
logger.info("##############################")
logger.info("#   Scenario completed       #")
logger.info("##############################")
