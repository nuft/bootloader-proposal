#!/usr/bin/env python3
"""
Update firmware using CVRA bootloading protocol.
"""
import logging
import commands
import msgpack
from zlib import crc32
from sys import exit

import utils
import progressbar
import sys
import yaml

def parse_commandline_args(args=None):
    """
    Parses the program commandline arguments.
    Args must be an array containing all arguments.
    """
    parser = utils.ConnectionArgumentParser(description=__doc__)
    parser.add_argument('-b', '--binary', dest='binary_file',
                        help='Path to the binary file to upload',
                        required=True,
                        metavar='FILE')
    parser.add_argument("-t", "--target",
                        help="YAML file containing target info",
                        required=True,
                        metavar='TARGET')
    parser.add_argument('-r', '--run',
                        help='Run application after flashing',
                        action='store_true')
    parser.add_argument("ids",
                        metavar='DEVICEID',
                        nargs='+', type=int,
                        help="Device IDs to flash")

    return parser.parse_args(args)

def slice_into_chunks(data, chunk_size):
    """
    Slices data into chunks that are at max chunk_size big.
    """
    while len(data) > chunk_size:
        yield data[:chunk_size]
        data = data[chunk_size:]
    yield data

class FlashSizeError(RuntimeError):
    """
    Error raised when the binary does not fit into the target's flash
    """
    pass

def pages_to_be_erased(target, length):
    pages = target['flash_pages']
    base = target['base_address']

    # ignore bootloader and config pages
    addr = pages[0][0]
    while addr < base:
        pages.pop(0)
        addr = pages[0][0]

    if pages[-1][0] - pages[0][0] + pages[0][1] < length:
        raise FlashSizeError

    erase = []
    for [addr, size] in pages:
        if length <= 0:
            break
        erase.append([addr, size])
        length -= size

    return erase

def flash_binary(conn, binary, target, destinations):
    """
    Writes a full binary to the flash using the given CAN connection.

    It takes target description dict as argument.
    """

    print("Erasing pages...")
    pbar = progressbar.ProgressBar(maxval=len(binary)).start()
    count = 0

    pages = pages_to_be_erased(target, len(binary))

    # First erase all pages
    for [page, length] in pages:
        erase_command = commands.encode_erase_flash_page(page, target['device_class'])
        print('write_command_retry', erase_command)
        res = utils.write_command_retry(conn, erase_command, destinations)

        failed_boards = [str(id) for id, success in res.items()
                         if not msgpack.unpackb(success)]

        if failed_boards:
            msg = ", ".join(failed_boards)
            msg = "Boards {} failed during page erase, aborting...".format(msg)
            logging.critical(msg)
            sys.exit(2)

        count += length
        pbar.update(count)

    pbar.finish()

    print("Writing pages...")
    pbar = progressbar.ProgressBar(maxval=len(binary)).start()

    # Then write all pages in chunks
    for offset, chunk in enumerate(slice_into_chunks(binary, target['chunk_size'])):
        offset *= target['chunk_size']
        command = commands.encode_write_flash(chunk,
                                              target['base_address'] + offset,
                                              target['device_class'])

        res = utils.write_command_retry(conn, command, destinations)
        failed_boards = [str(id) for id, success in res.items()
                         if not msgpack.unpackb(success)]

        if failed_boards:
            msg = ", ".join(failed_boards)
            msg = "Boards {} failed during page write, aborting...".format(msg)
            logging.critical(msg)
            sys.exit(2)

        pbar.update(offset)
    pbar.finish()

    # Finally update application CRC and size in config
    config = dict()
    config['application_size'] = len(binary)
    config['application_crc'] = crc32(binary)
    utils.config_update_and_save(conn, config, destinations)


def check_binary(conn, binary, base_address, destinations):
    """
    Check that the binary was correctly written to all destinations.

    Returns a list of all nodes which are passing the test.
    """
    valid_nodes = []

    expected_crc = crc32(binary)

    command = commands.encode_crc_region(base_address, len(binary))
    utils.write_command(conn, command, destinations)

    reader = utils.read_can_datagrams(conn)

    boards_checked = 0

    while boards_checked < len(destinations):
        dt = next(reader)

        if dt is None:
            continue

        answer, _, src = dt

        crc = msgpack.unpackb(answer)

        if crc == expected_crc:
            valid_nodes.append(src)

        boards_checked += 1

    return valid_nodes


def run_application(conn, destinations):
    """
    Asks the given node to run the application.
    """
    command = commands.encode_jump_to_main()
    utils.write_command(conn, command, destinations)


def verification_failed(failed_nodes):
    """
    Prints a message about the verification failing and exits
    """
    error_msg = "Verification failed for nodes {}" \
                .format(", ".join(str(x) for x in failed_nodes))
    print(error_msg)
    exit(1)


def check_online_boards(conn, boards):
    """
    Returns a set containing the online boards.
    """
    online_boards = set()

    utils.write_command(conn, commands.encode_ping(), boards)
    reader = utils.read_can_datagrams(conn)

    for dt in reader:
        if dt is None:
            break
        _, _, src = dt
        online_boards.add(src)

    return online_boards

def read_target_file(path):
    print('!!!!!!!!!!!!!!!!!')
    with open(path, 'r') as file:
        return yaml.load(file.read())

def main():
    """
    Entry point of the application.
    """
    args = parse_commandline_args()
    with open(args.binary_file, 'rb') as input_file:
        binary = input_file.read()

    conn = utils.open_connection(args)

    target = read_target_file(args.target)

    online_boards = check_online_boards(conn, args.ids)

    if online_boards != set(args.ids):
        offline_boards = [str(i) for i in set(args.ids) - online_boards]
        print("Boards {} are offline, aborting..."
              .format(", ".join(offline_boards)))
        exit(2)

    print("Flashing firmware (size: {} bytes)".format(len(binary)))
    flash_binary(conn, binary, target, args.ids)

    print("Verifying firmware...")
    valid_nodes_set = set(check_binary(conn, binary, target['base_address'], args.ids))
    nodes_set = set(args.ids)

    if valid_nodes_set == nodes_set:
        print("OK")
    else:
        verification_failed(nodes_set - valid_nodes_set)

    if args.run:
        run_application(conn, args.ids)


if __name__ == "__main__":
    main()
