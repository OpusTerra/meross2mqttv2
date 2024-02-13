# meross2mqtt v2

Meross MQTT bridge for OpenHAB 3 on Linux

Using excellent Python library MerossIot from albertogeniola
https://github.com/albertogeniola/MerossIot

and the original script code from daJoe Johannes R
Meross: python library with mqtt
https://community.openhab.org/t/meross-python-library-with-mqtt/83362

extensively inspired by excellent code from dmorlock Daniel Morlock
https://gitlab.awesome-it.de/daniel.morlock/meross-bridge

Here is an upgraded version of the script for Python3.9 using MerossIot V 0.4 Python3.9 with async fucntions
Please refer to sample_meross2mqttV2.service as an example on how to use this application as a service
## Changelog

#### 2.3

- This version breaks backward compatibility with Meross LOGIN method. When upgrading to this version, 
make sure to pass the new api_base_url as a parameter --api_base_url
    - Asia-Pacific: "iotx-ap.meross.com"
    - Europe: "iotx-eu.meross.com"
    - US: "iotx-us.meross.com".


