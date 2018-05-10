from __future__ import (unicode_literals, absolute_import,
                        division, print_function)
import os
import subprocess
import collections
import sys
import socket
import multiprocessing
import paramiko
import psutil
import re
import random
import time
import threading
import posixpath
from util import ONE_GB, ONE_MB, get_friendly_size
from .util import startup_info_args
from .util import subprocess_pretty_check_call

timeout = 10*60

if os.name == "nt":
    qemu_system_arm_exe = "qemu\qemu-system-arm.exe"
    qemu_img_exe = "qemu\qemu-img.exe"
else:
    qemu_system_arm_exe = "qemu-system-arm"
    qemu_img_exe = "qemu-img"

if getattr(sys, "frozen", False):
    bin_path = sys._MEIPASS
else:
    bin_path = ""

qemu_system_arm_exe_path = os.path.join(bin_path, qemu_system_arm_exe)
qemu_img_exe_path = os.path.join(bin_path, qemu_img_exe)
nb_cpus = multiprocessing.cpu_count()
qemu_cpu = nb_cpus - 1 if nb_cpus >= 2 else nb_cpus
host_ram = int(psutil.virtual_memory().total)

class QemuException(Exception):
    def __init__(self, msg):
        Exception(self, msg)

def generate_random_name():
    r = ""
    for _ in range(1,32):
        r += random.choice("0123456789ABCDEF")
    return r

def get_free_port():
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port

class Emulator:
    _image = None
    _kernel = None
    _dtb = None
    _logger = None

    # login=pi
    # password=raspberry
    # prompt end by ":~$ "
    # sudo doesn't require password
    def __init__(self, kernel, dtb, image, ram, logger):
        self._kernel = kernel
        self._dtb = dtb
        self._image = image
        self._logger = logger
        self._binary = qemu_system_arm_exe_path
        self._set_ram(ram.lower())

    def _set_ram(self, requested_ram):
        ''' applies requested RAM to qemu if it's available otherwise less '''

        # less than a GB is very short
        if host_ram / ONE_GB <= 1.0:
            self._ram = '256m'
            return

        # at most, use RAM - 512m
        max_ram = int(host_ram - ONE_GB / 2)

        if re.match(r'\d+[mg]$', requested_ram):
            ram_amount, ram_unit = int(requested_ram[:-1]), requested_ram[-1]
        else:
            # no unit, assuming M
            ram_amount, ram_unit = int(requested_ram), 'm'
        if ram_unit == 'g':
            ram_amount = ram_amount * (ONE_GB)

        # use requested if it doesn't exceed max_ram
        ram = max_ram if ram_amount > max_ram else ram_amount

        # vexpress-a15 is capped at 30G
        if int(ram / ONE_GB) > 30:
            ram = 30 * ONE_GB

        self._ram = "{ram}M".format(ram=int(ram / ONE_MB))
        self._logger.std(". using {ram} RAM".format(ram=get_friendly_size(ram)))

    def run(self, cancel_event):
        return _RunningInstance(self, self._logger, cancel_event)

    def get_image_size(self):
        output = subprocess_pretty_check_call([qemu_img_exe_path, "info", "-f", "raw", self._image], self._logger)
        matches = []
        for line_number, line in enumerate(output):
            if line_number == 2:
                matches = re.findall(b"virtual size: \S*G \((\d*) bytes\)", line)

        if len(matches) != 1:
            raise QemuException("cannot get image %s size from qemu-img info" % self._image)
        return int(matches[0])

    def resize_image(self, size):
        subprocess_pretty_check_call([qemu_img_exe_path, "resize", "-f", "raw", self._image, "{}".format(size)], self._logger)

    def copy_image(self, device_name):
        self._logger.step("Copy image to sd card")

        if os.name == "posix":
            image = os.open(self._image, os.O_RDONLY)
            device = os.open(device_name, os.O_WRONLY)
        elif os.name == "nt":
            image = os.open(self._image, os.O_RDONLY | os.O_BINARY)
            device = os.open(device_name, os.O_WRONLY | os.O_BINARY)
        else:
            self._logger.err("platform not supported")
            return

        total_size = os.lseek(image, 0, os.SEEK_END)
        os.lseek(image, 0, os.SEEK_SET)

        current_percentage = 0.0

        while True:
            current_size = os.lseek(image, 0, os.SEEK_CUR)
            new_percentage = (100 * current_size) / total_size
            if new_percentage != current_percentage:
                current_percentage = new_percentage
                self._logger.std(str(current_percentage) + "%\r")

            buf = os.read(image, 4096)
            if buf == b"":
                break
            os.write(device, buf)

        os.close(image)
        self._logger.step("Sync")
        os.fsync(device)
        os.close(device)

class _RunningInstance:
    _emulation = None
    _qemu = None
    _client = None
    _logger = None
    _cancel_event = None

    def __init__(self, emulation, logger, cancel_event):
        self._emulation = emulation
        self._logger = logger
        self._cancel_event = cancel_event

    def __enter__(self):
        try:
            self._boot()
        except:
            if self._qemu:
                self._qemu.kill()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self._shutdown()
        except:
            if self._qemu:
                self._qemu.kill()
            raise

    def _wait_signal(self, reader_fd, writer_fd, signal, timeout, return_buf_states_on_timeout=False):
        timeout_event = threading.Event()

        if return_buf_states_on_timeout:
            buf_states = []

        def raise_timeout():
            timeout_event.set()
            os.write(writer_fd, b"\nPibox installer: QEMU boot timeout\n")

        ring_buf = collections.deque(maxlen=len(signal))
        while True:
            timer = threading.Timer(timeout, raise_timeout)
            timer.start()
            buf = os.read(reader_fd, 1024)

            ring_buf.extend(buf)
            if return_buf_states_on_timeout:
                buf_states.append(buf)

            try:
                decoded_buf = buf.decode("utf-8")
            except:
                pass
            else:
                self._logger.raw_std(decoded_buf)

            if timeout_event.is_set():
                if return_buf_states_on_timeout:
                    return buf_states
                else:
                    raise QemuException("wait signal timeout: %s" % signal)

            timer.cancel()

            if list(ring_buf) == list(signal):
                return

    def _boot(self):
        ssh_port = get_free_port()

        stdout_reader, stdout_writer = os.pipe()
        stdin_reader, stdin_writer = os.pipe()

        self._logger.step("Launch qemu")
        self._logger.std("ssh on port {}".format(ssh_port))

        with self._cancel_event.lock() as cancel_register:
            command = [
                self._emulation._binary,
                "-m", self._emulation._ram,
                "-M", "vexpress-a15",
                "-kernel", self._emulation._kernel,
                "-dtb", self._emulation._dtb,
                "-append", "root=/dev/mmcblk0p2 console=ttyAMA0 console=tty",
                "-serial", "stdio",
                "-drive", "format=raw,if=sd,file={}"
                .format(self._emulation._image),
                "-netdev", "user,id=eth0,hostfwd=tcp::{}-:22".format(ssh_port),
                "-device", "virtio-net-device,netdev=eth0",
                "-display", "none",
                "-no-reboot", "-no-acpi",
            ]
            if qemu_cpu > 1:
                command += ["-smp", str(qemu_cpu),
                            "--accel", "tcg,thread=multi"]
            self._logger.std("--\n{}\n--".format(" ".join(command)))

            self._qemu = subprocess.Popen(
                command,
                stdin=stdin_reader, stdout=stdout_writer,
                stderr=subprocess.STDOUT, **startup_info_args())
            cancel_register.register(self._qemu.pid)

        self._wait_signal(stdout_reader, stdout_writer, b"login: ", timeout)
        os.write(stdin_writer, b"pi\n")

        tries = 0
        while True:
            signal = b"Password: "
            buf_states = self._wait_signal(stdout_reader, stdout_writer, signal, timeout, True)

            if not buf_states:
                break

            self._logger.err(str(buf_states))
            self._logger.err("internal error: please report this log to pibox-installer")
            if tries > 3:
                raise QemuException("wait signal timeout: %s" % signal)
            os.write(stdin_writer, b"pi\n")
            tries += 1

        os.write(stdin_writer, b"raspberry\n")
        self._wait_signal(stdout_reader, stdout_writer, b":~$ ", timeout)
        # TODO: This is a ugly hack. But writing all at once doesn't work
        os.write(stdin_writer, b"sudo systemctl")
        self._wait_signal(stdout_reader, stdout_writer, b"sudo systemctl", timeout)
        os.write(stdin_writer, b" start ssh;")
        self._wait_signal(stdout_reader, stdout_writer, b" start ssh;", timeout)
        os.write(stdin_writer, b" exit\n")
        self._wait_signal(stdout_reader, stdout_writer, b"login: ", timeout)

        time.sleep(10);

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.MissingHostKeyPolicy())
        self._client.connect("localhost", port=ssh_port,
                             username="pi", password="raspberry",
                             allow_agent=False, look_for_keys=False)

    def _shutdown(self):
        self.exec_cmd("sudo shutdown -P 0")
        self._client.close()
        self._qemu.wait()  # TODO: implement timeout
        with self._cancel_event.lock() as cancel_register:
            cancel_register.unregister(self._qemu.pid)
        self._qemu = None

    # The remote path must not exist
    def put_dir(self, localpath, remotepath):
        # We first copy to a temporary path we have right on
        # then we copy to final path with sudo call
        sftp_client = self._client.open_sftp()
        tmpremotepath = "/tmp/" + generate_random_name()
        sftp_client.mkdir(tmpremotepath)
        self._logger.std("copy local dir {} to tmp dir {}".format(localpath, tmpremotepath))

        old_cwd = os.getcwd()
        os.chdir(localpath)
        for localdirpath, dirnames, filenames in os.walk("."):
            remotedirpath = ''
            remotedirpath_unformatted = localdirpath

            while remotedirpath_unformatted is not '':
                remotedirpath_unformatted, tail = os.path.split(remotedirpath_unformatted)
                remotedirpath = posixpath.join(tail, remotedirpath)

            for dirname in dirnames:
                dir_remote_path = posixpath.join(tmpremotepath, remotedirpath, dirname)
                self._logger.std("make remote dir {}".format(dir_remote_path))
                sftp_client.mkdir(dir_remote_path)

            for filename in filenames:
                file_local_path = os.path.normpath(
                    os.path.join(localdirpath, filename))
                file_remote_path = os.path.normpath(
                    posixpath.join(tmpremotepath, remotedirpath, filename))
                self._logger.std("copy local file {} to tmp file {}".format(file_local_path, file_remote_path))
                sftp_client.put(file_local_path, file_remote_path)
        sftp_client.close()
        os.chdir(old_cwd)

        self.exec_cmd("sudo mv -T {} {}".format(tmpremotepath, remotepath))

    # The remote path must be a file
    def put_file(self, localpath, remotepath):
        # We first copy to a temporary path we have right on
        # then we move to final path with sudo call
        sftp_client = self._client.open_sftp()
        tmpremotepath = "/tmp/" + generate_random_name()
        self._logger.std("copy local file {} to tmp file {}".format(localpath, tmpremotepath))
        sftp_client.put(localpath, tmpremotepath)
        sftp_client.close()
        self.exec_cmd("sudo mv -T {} {}".format(tmpremotepath, remotepath))

    def exec_cmd(self, command, displayed_command=None, capture_stdout=False, check=True, show_command=True):
        if capture_stdout:
            stdout_buffer = ""

        if show_command:
            if displayed_command is None:
                displayed_command = command
            self._logger.std(displayed_command)
        _, stdout, stderr = self._client.exec_command(command)
        while True:
            line = stdout.readline()
            if line == "":
                break
            if capture_stdout:
                stdout_buffer += line
            self._logger.std(line.replace("\n", ""))

        for line in stderr.readlines():
            self._logger.err("STDERR: " + line.replace("\n", ""))

        exit_status = stdout.channel.recv_exit_status();
        if exit_status != 0 and check:
            raise QemuException("ssh command failed with status {}. cmd: {}".format(exit_status, command))

        if capture_stdout:
            return stdout_buffer

    def _reboot(self):
        self._logger.std("reboot qemu")
        self._shutdown()
        self._boot()
