from __future__ import absolute_import
from fortrace.botnet.bots.hellobot.hello_bot import HelloBotMaster
from six.moves import input

__author__ = 'Sascha Kopp'

b = HelloBotMaster()
b.start()
input("press enter to exit:\n")
b.stop()
