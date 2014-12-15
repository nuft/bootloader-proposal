import sys
import serial
import can
import can_bridge
import commands
import serial_datagrams

import binascii

def write_frame(fdesc, frame):
    bridge_frame = can_bridge.encode_frame_command(frame)
    datagram = serial_datagrams.datagram_encode(bridge_frame)
    fdesc.write(datagram)
    # fdesc.flush()

def write_command(fdesc, command):
    datagram = can.encode_datagram(command, [0x2a])
    frames = can.datagram_to_frames(datagram, 0x42)
    for f in frames:
        write_frame(fdesc, f)

if len(sys.argv) < 3:
    print("yo!")
    sys.exit(1)


baud = 115200
fd = serial.Serial(sys.argv[1], baudrate=baud)

with open(sys.argv[2], "rb") as f:
    binary = f.read()

write_flash = commands.encode_write_flash(binary, 0x08003000, "servoboard.v1")
# print(binascii.hexlify(write_flash))
print("write_flash")
write_command(fd, write_flash)

import time
time.sleep(1)

app_jmp = commands.encode_jump_to_main()
# print(binascii.hexlify(app_jmp))
print("app_jmp")
write_command(fd, app_jmp)

