#!/usr/local/bin/python3.9

import signal
import os
import paho.mqtt.client as mqtt
from meross_iot.manager import MerossManager
from meross_iot.http_api import MerossHttpClient
from meross_iot.model.enums import OnlineStatus, Namespace
import json
import asyncio
import argparse
import logging
import sys
from queue import Queue, Empty
from meross_iot.model.push.generic import GenericPushNotification
from meross_iot.controller.device import BaseDevice

# Denis Lambert - Janv 2021
# Using meross_iot 0.4
# https://github.com/albertogeniola/MerossIot
# and extensively based on
# https://community.openhab.org/t/meross-python-library-with-mqtt/83362
# and
# https://gitlab.awesome-it.de/daniel.morlock/meross-bridge

def getState(d, cNo=None):
    status = False

    if d.online_status == OnlineStatus.ONLINE:
        if cNo is None:
            status = d.is_on()
        else:
            status = d.is_on(cNo)

    if status:
        return "ON"
    return "OFF"


def getChannelName(d, cNo):
    return d.channels[cNo].name


def getJsonMsg(d, cNo=None):
    # {"state": "ON", "online": true, "type": "mss550x", "friendlyName": "perron jaune"}
    s = getChannelName(d, cNo)
    info = {"state": getState(d, cNo), "type": d.type, "ChannelName": s, "friendlyName": d.name,
            "online": d.online_status == OnlineStatus.ONLINE}

    return json.dumps(info)


class MerossOpenHabBridge:

    def __init__(self, args, log, loop):
        self.args = args

        self.log = log
        self.client = None
        self.meross_api = None
        self.manager = None
        self.loop = loop
        self.queue = Queue()
        self.stopped = False
        self.mqtt_auth = self.args.mqtt_usr is not None and self.args.mqtt_pswd is not None

    async def start(self):

        self.log.info("Starting Meross <> OpenHAB task")

        self.client = mqtt.Client(self.args.mqtt_ident, clean_session=True)

        mqtt_password = self.args.mqtt_pswd or os.environ.get('OPENHAB_MQTT_PASSWORD')

        if self.mqtt_auth:
            self.client.username_pw_set(self.args.mqtt_usr, password=mqtt_password)

        lwm = "Meross Gone Offline"  # Last will message
        self.log.info("Setting Last will message=", lwm, "topic is", self.args.mqtt_ident)
        self.client.will_set(self.args.mqtt_ident, lwm, qos=1, retain=False)

        self.client.connect(self.args.mqtt_server)
        self.client.publish(self.args.mqtt_ident, payload=json.dumps({"state": True}), qos=0, retain=True)

        self.client.loop_start()
        self.client.on_disconnect = self.reconnect_mqtt_client
        self.client.on_message = self.handle_openhab_mqtt_message

        password = self.args.password or os.environ.get('MEROSS_PASSWORD')
        self.meross_api = await MerossHttpClient.async_from_user_password(email=self.args.email, password=password)
        self.manager = MerossManager(http_client=self.meross_api)
        self.manager.register_push_notification_handler_coroutine(self.event_handler)

        await self.manager.async_init()

        # Discover devices.
        await self.manager.async_device_discovery()

        # subscribe to OpenHAB broker topic to control Meross Devices
        # as well as auto publish actual devices states
        await self.subscribe_broker()

    async def stop(self):
        self.client.publish(self.args.mqtt_ident, payload=json.dumps({"state": False}), qos=0, retain=True)
        self.stopped = True
        self.manager.close()
        await self.meross_api.async_logout()
        self.client.loop_stop()
        self.client.disconnect()

    def reconnect_mqtt_client(self, client, userdata, rc):
        if rc != 0:
            self.log.warning("Unexpected MQTT disconnect, auto re-connect ...")
            self.client.reconnect()

    # internal loop waiting for messages published on OpenHAB MQTT
    async def consume(self):
        self.log.info("Starting consumer for OpenHAB MQTT messages ...")
        while not self.stopped:
            try:
                (client, userdata, message) = self.queue.get(block=False)
                await self.async_handle_openhab_mqtt_message(client, userdata, message)
            except Empty:
                await asyncio.sleep(1)
                continue

    # Message received from OpenHAB MQTT to be forwarded to a Meross devices (via MQTT)
    # Put in queue and consumed eventually by the queue loop above
    def handle_openhab_mqtt_message(self, client, userdata, message):
        self.queue.put_nowait((client, userdata, message))

    # General processing of the Message received from OpenHAB MQTT
    async def async_handle_openhab_mqtt_message(self, client, userdata, message):

        payload = str(message.payload.decode("utf-8"))
        self.log.info(f'Handling message from topic "{message.topic}": {payload}  with qos {message.qos} and retain flag {message.retain}')

        msg_split = message.topic.split("/")
        if len(msg_split) > 2:
            if msg_split[0] == self.args.mqtt_ident and msg_split[-1] == "set":
                await self.handle_message("/".join(msg_split[1:-1]), str(message.payload.decode("utf-8")))
        elif msg_split[0] == self.args.mqtt_ident and msg_split[1] == "control":
            self.log.info("Receiving MQTT message from OpenHAB for future Meross controlling")

        # Device level processing of the Message received from OpenHAB MQTT
    # todo: Add support to change of color bulb color from related color bulb topic
    async def handle_message(self, topic, messageStr):
        # print("topic %s -> handling message: %s" % (topic, messageStr))
        try:
            # si erreur dans le format du json recu
            msg = json.loads(messageStr)

            topic_split = topic.split("/")
            device_name = topic_split[0]

            if self.manager is not None:
                device = self.manager.find_devices(device_name=device_name, online_status=OnlineStatus.ONLINE)
                if device is not None:
                    if len(topic_split) == 1:
                        if 'state' in msg:
                            if msg['state'] == 'ON':
                                await device[0].async_turn_on(channel=0)
                            elif msg['state'] == 'OFF':
                                await device[0].async_turn_off(channel=0)
                    elif len(topic_split) == 2:
                        channel_name = topic_split[1]
                        cNo = -1
                        if channel_name.startswith("channel_"):
                            cNo = int(channel_name.replace("channel_", ""))
                        else:
                            channels = device.channels
                            for i in range(len(channels)):
                                if 'devName' in channels[i] and channels[i]['devName'] == channel_name:
                                    cNo = i
                        if cNo > -1:
                            if 'state' in msg:
                                if msg['state'] == 'ON':
                                    await device[0].async_turn_on(channel=cNo)
                                elif msg['state'] == 'OFF':
                                    await device[0].async_turn_off(channel=cNo)
                        else:
                            self.log.error("Channel '%s' not found for device '%s'!" % (topic_split[1], device_name))
                else:
                    self.log.error("Device '%s' not found !" % device_name)

        except json.decoder.JSONDecodeError:
            self.log.error("String %s could not be converted to JSON" % messageStr)
        except:
            self.log.error("Unexpected error in MQTT message handling:", sys.exc_info()[0])

    # setup topics to be listened to when OpenHAB wants to to send a command to a Meross Device
    # no channel device topic format is meross/device name/set
    # and channel device is meross/device name/channel_x/set
    # where the expected json message is
    # { "state" : "OFF"} or { "state" : "ON"}
    # samples topics: meross/color-bulb/set, meross/Smart Outdoor Plug 2/channel_1/set
    # todo: Add topic to support change of color bulb color
    async def subscribe_broker(self):
        # print("All the supported devices I found:")
        all_devices = self.manager.find_devices()
        self.log.info("Subscribing to OpenHab MQTT:")
        for d in all_devices:
            # next statement is mandadory
            # ERROR:Please invoke async_update() for this device (couleur) before accessing its state. Failure to do so may result in inconsistent state.
            await d.async_update()  # fetch the complete device state.

            if len(d.channels) > 1:
                channels = d.channels
                for i in range(len(channels)):
                    self.client.subscribe("meross/%s/channel_%i/set" % (d.name, i))
                    self.log.info("Topic: meross/%s/channel_%i/set" % (d.name, i))

                    self.client.publish("meross/%s/channel_%s" % (d.name, i), getJsonMsg(d, i))
            else:
                self.client.subscribe("meross/%s/set" % d.name)
                self.log.info("Topic: meross/%s/set" % d.name)
                self.client.publish("meross/%s/channel_%s" % (d.name, 0), getJsonMsg(d, 0))

        # MQTT message from OpenHAB for future Meross control
        self.client.subscribe("meross/control")

    # Message handler from Meross MQTT
    # will be notified when there is a device change anywhere in the cloud
    async def event_handler(self, push_notification: GenericPushNotification, target_devices: [BaseDevice]):
        try:
            if push_notification.namespace == Namespace.CONTROL_BIND \
                    or push_notification.namespace == Namespace.SYSTEM_ONLINE \
                    or push_notification.namespace == Namespace.HUB_ONLINE:

                self.log.info(f'push notification from namespace  {str(push_notification.namespace)}')

                # TODO: Discovery needed only when device becomes online?
                # TODO add subscribing topic for newly found device
                # d.online_status == OnlineStatus.ONLINE
                await self.manager.async_device_discovery(meross_device_uuid=push_notification.originating_device_uuid)

                # va retourner seulement le device qui possède ce uuid
                # devs = self.manager.find_devices(device_uuids=(push_notification.originating_device_uuid,))
                # await self.subscribe_broker()

            elif push_notification.namespace == Namespace.CONTROL_TOGGLEX:
                if len(target_devices) > 0:
                    if not target_devices[0].check_full_update_done():
                        await target_devices[0].async_update()  # fetch the complete device state.

                    '''
                    # sample logs
                    # WARNING:push notification for perron jaune from namespace  "Namespace.CONTROL_TOGGLEX": {'togglex': [{'onoff': 1, 'lmTime': 1610111826, 'channel': 0}]}
                    # WARNING:push notification for Smart Outdoor Plug 2 from namespace  "Namespace.CONTROL_TOGGLEX": {'togglex': {'onoff': 1, 'lmTime': 1610111898, 'channel': 2}}
                    '''
                    self.log.info(
                        f'push notification for {target_devices[0].name} from namespace  "{str(push_notification.namespace)}": {str(push_notification.raw_data)}')

                    '''
                    # samples topic publishing
                    
                    # meross/perron jaune/channel_0
                    # {"state": "OFF", "type": "mss550x", "ChannelName": "Main channel", "friendlyName": "perron jaune", "online": true}
        
                    # meross/Smart Outdoor Plug 2/channel_1
                    # {"state": "OFF", "type": "mss620", "ChannelName": "Switch 1", "friendlyName": "Smart Outdoor Plug 2", "online": true}
                    
                    # meross/couleur/channel_0
                    # {"state": "OFF", "type": "msl120j", "ChannelName": "Main channel", "friendlyName": "couleur", "online": true}
                    '''
                    cTog = push_notification.raw_data.get('togglex', None)
                    if type(cTog) is dict:
                        cNo = cTog.get('channel', None)
                    else:
                        cNo = cTog[0]['channel']

                    self.client.publish("meross/%s/channel_%s" % (target_devices[0].name, cNo), getJsonMsg(target_devices[0], cNo))

            elif push_notification.namespace == Namespace.CONTROL_LIGHT:
                if len(target_devices) > 0:
                    if not target_devices[0].check_full_update_done():
                        await target_devices[0].async_update()  # fetch the complete device state.
                    '''
                    # sample log
                    # WARNING:push notification for couleur from namespace  "Namespace.CONTROL_LIGHT": {'light': {'transform': -1, 'temperature': 16, 'rgb': 16726272, 'luminance': 2, 'channel': 0, 'capacity': 6}}
                    '''
                    self.log.info(
                        f'push notification for {target_devices[0].name} from namespace  "{str(push_notification.namespace)}": {str(push_notification.raw_data)}')
            else:
                if len(target_devices) > 0:
                    self.log.warning(
                        f'Unknown push notification for {target_devices[0].name} from namespace  "{str(push_notification.namespace)}": {str(push_notification.raw_data)}')
                return
        except:
            self.log.error("Unexpected error in push notification event_handler:", sys.exc_info()[0])

def parse_command_line():
    parser = argparse.ArgumentParser(description='Meross MQTT bridge for OpenHAB')

    parser.add_argument('--mqtt-ident', dest='mqtt_ident', help='An unique mqtt ident to populate', default='meross')
    parser.add_argument('--mqtt-server', dest='mqtt_server', help='Hostname or IP address of mqtt server', default='localhost')

    parser.add_argument('--mqtt_usr', dest='mqtt_usr', help='Username for OpenHAB MQTT broker', default=None)
    parser.add_argument('--mqtt_pswd', dest='mqtt_pswd',
                        help='Password for  OpenHAB MQTT broker (can be specified by environment variable OPENHAB_MQTT_PASSWORD)',
                        default=None)

    parser.add_argument('-e', '--email', dest='email', help='Email/Username for Meross manager', required=True)
    parser.add_argument('-p', '--password', dest='password',
                        help='Password for Meross manager (can be specified by environment variable MEROSS_PASSWORD)',
                        default=None)

    parser.add_argument('-l', '--log', dest='logfile', help="Path to logfile")
    parser.add_argument('-v', '--verbose', action='store_true')

    return parser.parse_args()


class Runner:

    def __init__(self):
        self.args = parse_command_line()
        self.log = self.setup_logging()
        self.bridge = None

        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def run(self):
        loop = asyncio.get_event_loop()
        self.bridge = MerossOpenHabBridge(self.args, self.log, loop)

        try:
            loop.run_until_complete(self.bridge.start())
            loop.run_until_complete(self.bridge.consume())
        except KeyboardInterrupt:
            self.log.info('Interrupted by user, going down ...')
            pass
        finally:
            loop.run_until_complete(self.bridge.stop())
            loop.close()
        sys.exit(0)

    def exit_gracefully(self, signum, frame):
        self.log.info('captured signal %d' % signum)
        raise SystemExit

    def setup_logging(self):
        if self.args.verbose:
            loglevel = logging.DEBUG
        else:
            loglevel = logging.INFO

        # comment next 2 lines to get loggind at stdout
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        if self.args.logfile:
            logging.basicConfig(level=loglevel, filename=self.args.logfile,
                                format='%(asctime)s %(levelname)-8s %(name)-10s %(message)s')
        else:
            logging.basicConfig(level=loglevel, format='%(asctime)s %(levelname)-8s %(name)-10s %(message)s')

        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)
        logging.getLogger("meross").setLevel(logging.INFO)

        return logging.getLogger()


def main():
    Runner().run()


if __name__ == '__main__':
    main()
