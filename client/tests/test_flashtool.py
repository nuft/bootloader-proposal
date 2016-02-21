import unittest

try:
    from unittest.mock import *
except ImportError:
    from mock import *

from zlib import crc32

from bootloader_flash import *
from commands import *
from utils import *
import msgpack

from io import BytesIO

import sys

@patch('utils.write_command_retry')
class FlashBinaryTestCase(unittest.TestCase):
    fd = "port"

    def setUp(self):
        mock = lambda m: patch(m).start()
        self.progressbar = mock('progressbar.ProgressBar')
        # self.print = mock('builtins.print')
        self.target = {'device_class': 'dummy',
                       'base_address': 0x1000,
                        'chunk_size': 2048,
                        'flash_pages': [
                            [0x0000, 0x1000],
                            [0x1000, 0x1000],
                            [0x2000, 0x1000]
                         ]}

    def tearDown(self):
        patch.stopall()

    def test_single_page_erase(self, write):
        """
        Checks that a single page is erased before writing.
        """
        data = bytes(range(20))
        destinations = [1]

        flash_binary(self.fd, data, self.target, destinations)

        erase_command = encode_erase_flash_page(self.target['base_address'],
                                                self.target['device_class'])
        write.assert_any_call(self.fd, erase_command, destinations)

    def test_write_single_chunk(self, write):
        """
        Tests that a single chunk can be written.
        """
        data = bytes(range(20))
        destinations = [1]

        flash_binary(self.fd, data, self.target, [1])

        write_command = encode_write_flash(data, self.target['base_address'], self.target['device_class'])

        write.assert_any_call(self.fd, write_command, destinations)

    def test_write_many_chunks(self, write):
        """
        Checks that we can write many chunks, but still in one page
        """
        data = bytes([0] * 4096)
        address = self.target['base_address']
        device_class = self.target['device_class']
        destinations = [1]

        flash_binary(self.fd, data, self.target, [1])

        write_command = encode_write_flash(bytes([0] * 2048), address, device_class)
        write.assert_any_call(self.fd, write_command, destinations)

        write_command = encode_write_flash(bytes([0] * 2048), address + 2048, device_class)
        write.assert_any_call(self.fd, write_command, destinations)

    def test_erase_multiple_pages(self, write):
        """
        Checks that all pages are erased before writing data to them.
        """
        data = bytes([0] * 4096 * 2)
        destinations = [1]

        flash_binary(self.fd, data, self.target, destinations)

        # Check that all pages were erased correctly
        for addr in [0x1000, 0x2000]:
            erase_command = encode_erase_flash_page(addr, self.target['device_class'])
            write.assert_any_call(self.fd, erase_command, destinations)

    @patch('utils.config_update_and_save')
    def test_crc_is_updated(self, conf, write):
        """
        Tests that the CRC is updated after flashing a binary.
        """
        data = bytes([0] * 10)
        dst = [1]

        flash_binary(self.fd, data, self.target, dst)

        expected_config = {'application_size': 10, 'application_crc': crc32(data)}
        conf.assert_any_call(self.fd, expected_config, dst)

    @patch('logging.critical')
    def test_bad_board_page_erase(self, c, write):
        """
        Checks that a board who replies with an error flag during page erase
        leads to firmware upgrade halt.
        """
        ok, nok = msgpack.packb(True), msgpack.packb(False)
        write.return_value = {1: nok, 2: nok, 3: ok}  # Board 1 fails

        data = bytes([0] * 10)

        with self.assertRaises(SystemExit):
            flash_binary(None, data, self.target, [1, 2, 3])

        c.assert_any_call("Boards 1, 2 failed during page erase, aborting...")

    @patch('logging.critical')
    def test_bad_board_page_write(self, c, write):
        """
        In this scenario we test what happens if the page erase is OK, but then
        the page write fails.
        """
        ok, nok = msgpack.packb(True), msgpack.packb(False)
        side_effect = [{1: ok, 2: ok, 3: ok}]
        side_effect += [{1: nok, 2: nok, 3: ok}]  # Board 1 fails
        write.side_effect = side_effect

        data = bytes([0] * 10)

        with self.assertRaises(SystemExit):
            flash_binary(None, data, self.target, [1, 2, 3])

        c.assert_any_call("Boards 1, 2 failed during page write, aborting...")


class ConfigTestCase(unittest.TestCase):
    fd = "port"

    @patch('utils.write_command_retry')
    def test_config_is_updated_and_saved(self, write):
        """
        Checks that the config is correctly sent encoded to the board.
        We then check if the config is saved to flash.
        """
        config = {'id':14}
        dst = [1]
        update_call = call(self.fd, encode_update_config(config), dst)
        save_command = call(self.fd, encode_save_config(), dst)

        config_update_and_save(self.fd, config, [1])

        # Checks that the calls were made, and in the correct order
        write.assert_has_calls([update_call, save_command])

    @patch('utils.read_can_datagrams')
    @patch('utils.write_command')
    def test_check_single_valid_checksum(self, write, read_datagram):
        """
        Checks what happens if there are invalid checksums.
        """
        binary = bytes([0] * 10)
        crc = crc32(binary)

        side_effect  = [(msgpack.packb(crc), [0], 1)]
        side_effect += [(msgpack.packb(0xdead), [0], 2)]
        side_effect += [None] # timeout

        read_datagram.return_value = iter(side_effect)


        valid_nodes = check_binary(self.fd, binary, 0x1000, [1, 2])

        self.assertEqual([1], valid_nodes)

    @patch('utils.read_can_datagrams')
    @patch('utils.write_command')
    def test_verify_handles_timeout(self, write, read_datagram):
        """
        When working with large firmwares, the connection may timeout before
        answering (issue #53).
        """
        binary = bytes([0] * 10)
        crc = crc32(binary)

        side_effect  = [None] # timeout
        side_effect += [(msgpack.packb(0xdead), [0], 2)]
        side_effect += [(msgpack.packb(crc), [0], 1)]

        read_datagram.return_value = iter(side_effect)

        valid_nodes = check_binary(self.fd, binary, 0x1000, [1, 2])

        self.assertEqual([1], valid_nodes)

class RunApplicationTestCase(unittest.TestCase):
    fd = 'port'

    @patch('utils.write_command')
    def test_run_application(self, write):
        run_application(self.fd, [1])

        command = commands.encode_jump_to_main()
        write.assert_any_call(self.fd, command, [1])

class MainTestCase(unittest.TestCase):
    """
    Tests for the main function of the program.

    Since the code has a good coverage and is quite complex, there is an
    extensive use of mocking in those tests to replace all collaborators.
    """
    def setUp(self):
        """
        Function that runs before each test.

        The main role of this function is to prepare all mocks that are used in
        main, setup fake file and devices etc.
        """
        mock = lambda m: patch(m).start()
        self.open = mock('builtins.open')
        self.print = mock('builtins.print')

        self.open_conn = mock('utils.open_connection')
        self.conn = Mock()
        self.open_conn.return_value = self.conn

        self.flash = mock('bootloader_flash.flash_binary')
        self.check = mock('bootloader_flash.check_binary')
        self.run = mock('bootloader_flash.run_application')

        self.check_online_boards = mock('bootloader_flash.check_online_boards')
        self.check_online_boards.side_effect = lambda f, b: set([1, 2, 3])

        self.target = {'device_class': 'dummy', 'base_address': 0x1000}
        self.read_target = mock('bootloader_flash.read_target_file')
        self.read_target.return_value = self.target

        # Prepare binary file argument
        self.binary_data = bytes([0] * 10)
        self.open.return_value = BytesIO(self.binary_data)

        # Flash checking results
        self.check.return_value = [1, 2, 3] # all boards are ok

        # Populate command line arguments
        sys.argv = "test.py -b test.bin -p /dev/ttyUSB0 -t dummy.yml 1 2 3".split()

    def tearDown(self):
        """
        function run after each test.
        """
        # Deactivate all mocks
        patch.stopall()

    def test_open_file(self):
        """
        Checks that the correct file is opened.
        """
        main()
        self.open.assert_any_call('test.bin', 'rb')

    def test_failing_ping(self):
        """
        Checks what happens if a board doesn't pingback.
        """
        # No board answers
        self.check_online_boards.side_effect = lambda f, b: set()

        with self.assertRaises(SystemExit):
            main()


    def test_flash_binary(self):
        """
        Checks that the binary file is flashed correctly.
        """
        main()
        self.flash.assert_any_call(self.conn, self.binary_data, self.target, [1,2,3])

    def test_check(self):
        """
        Checks that the flash is verified.
        """
        main()
        self.check.assert_any_call(self.conn, self.binary_data, self.target['base_address'], [1,2,3])


    def test_check_failed(self):
        """
        Checks that the program behaves correctly when verification fails.
        """
        self.check.return_value = [1]
        with patch('bootloader_flash.verification_failed') as failed:
            main()
            failed.assert_any_call(set((2,3)))

    def test_do_not_run_by_default(self):
        """
        Checks that by default no run command are ran.
        """
        main()
        self.assertFalse(self.run.called)

    def test_run_if_asked(self):
        """
        Checks if we can can ask the board to run.
        """
        sys.argv += ["--run"]
        main()
        self.run.assert_any_call(self.conn, [1, 2, 3])




    def test_verification_failed(self):
        """
        Checks that the verification failed method works as expected.
        """
        with self.assertRaises(SystemExit):
            verification_failed([1,2])

        self.print.assert_any_call('Verification failed for nodes 1, 2')

class ArgumentParsingTestCase(unittest.TestCase):
    """
    All tests related to argument parsing.
    """

    def test_simple_case(self):
        """
        Tests the most simple case.
        """
        commandline = "-b test.bin -p /dev/ttyUSB0 --run -t dummy.yml 1 2 3"
        args = parse_commandline_args(commandline.split())
        self.assertEqual('test.bin', args.binary_file)
        self.assertEqual('/dev/ttyUSB0', args.serial_device)
        self.assertEqual('dummy.yml', args.target)
        self.assertEqual([1,2,3], args.ids)
        self.assertTrue(args.run)

    def test_can_interface_argument(self):
        """
        Checks that we can pass CAN interface
        """
        commandline = "-b test.bin --interface /dev/can0 --run -t dummy.yml 1 2 3"
        args = parse_commandline_args(commandline.split())
        self.assertEqual(None, args.serial_device)
        self.assertEqual("/dev/can0", args.can_interface)

    def test_can_interface_or_serial_is_required(self):
        """
        Checks that we have either a serial device or a CAN interface to use.
        """
        commandline = "-b test.bin --run -t dummy.yml 1 2 3"

        with patch('argparse.ArgumentParser.error') as error:
            parse_commandline_args(commandline.split())

            # Checked that we printed some kind of error
            error.assert_any_call(ANY)

    def test_can_interface_or_serial_are_exclusive(self):
        """
        Checks that the serial device and the CAN interface are mutually exclusive.
        """
        commandline = "-b test.bin -p /dev/ttyUSB0 --interface /dev/can0 --run -t dummy.yml 1 2 3"

        with patch('argparse.ArgumentParser.error') as error:
            parse_commandline_args(commandline.split())

            # Checked that we printed some kind of error
            error.assert_any_call(ANY)


class PagesToBeErasedTestCase(unittest.TestCase):
    """
    Tests list of pages chosen to be erased.
    """

    target = {
        'base_address': 0x1000,
        'flash_pages': [
            [0x0000, 0x1000],
            [0x1000, 0x1000],
            [0x2000, 0x1000],
            [0x3000, 0x1000]
        ]
    }

    def test_less_than_one_page(self):
        less_than_one_page = 10
        pages = pages_to_be_erased(self.target, less_than_one_page)
        self.assertEqual(pages, [[0x1000, 0x1000]])

    def test_exactly_one_page(self):
        size_of_first_page = 0x1000
        pages = pages_to_be_erased(self.target, size_of_first_page)
        self.assertEqual(pages, [[0x1000, 0x1000]])

    def test_more_than_one_page(self):
        more_than_one_page = 0x1000 + 1
        pages = pages_to_be_erased(self.target, more_than_one_page)
        self.assertEqual(pages, [[0x1000, 0x1000],
                                 [0x2000, 0x1000]])

    def test_detect_flash_size_error(self):
        more_than_flash = 0x3000 + 1
        self.assertRaises(FlashSizeError, pages_to_be_erased, self.target, more_than_flash)


