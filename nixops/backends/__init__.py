# -*- coding: utf-8 -*-

import os
import re
import subprocess
from typing import Dict, Any, List, Optional, Union, Set
import nixops.util
import nixops.resources
import nixops.ssh_util
import xml.etree.ElementTree as ET


class MachineDefinition(nixops.resources.ResourceDefinition):
    """Base class for NixOps machine definitions."""

    def __init__(self, xml, config={}) -> None:
        nixops.resources.ResourceDefinition.__init__(self, xml, config)
        self.store_keys_on_machine = (
            xml.find("attrs/attr[@name='storeKeysOnMachine']/bool").get("value")
            == "true"
        )
        self.ssh_port = int(xml.find("attrs/attr[@name='targetPort']/int").get("value"))
        self.always_activate = (
            xml.find("attrs/attr[@name='alwaysActivate']/bool").get("value") == "true"
        )
        self.owners = [
            e.get("value")
            for e in xml.findall("attrs/attr[@name='owners']/list/string")
        ]
        self.has_fast_connection = (
            xml.find("attrs/attr[@name='hasFastConnection']/bool").get("value")
            == "true"
        )

        def _extract_key_options(x: ET.Element) -> Dict[str, str]:
            opts = {}
            for (key, xmlType) in (
                ("text", "string"),
                ("keyFile", "path"),
                ("destDir", "string"),
                ("user", "string"),
                ("group", "string"),
                ("permissions", "string"),
            ):
                elem = x.find("attrs/attr[@name='{0}']/{1}".format(key, xmlType))
                if elem is not None:
                    value = elem.get("value")
                    if value is not None:
                        opts[key] = value
            return opts

        self.keys = {
            k.get("name"): _extract_key_options(k)
            for k in xml.findall("attrs/attr[@name='keys']/attrs/attr")
        }


class MachineState(nixops.resources.ResourceState):
    """Base class for NixOps machine state objects."""

    vm_id: Optional[str] = nixops.util.attr_property("vmId", None)
    has_fast_connection: bool = nixops.util.attr_property(
        "hasFastConnection", False, bool
    )
    ssh_pinged: bool = nixops.util.attr_property("sshPinged", False, bool)
    ssh_port: int = nixops.util.attr_property("targetPort", 22, int)
    public_vpn_key: Optional[str] = nixops.util.attr_property("publicVpnKey", None)
    store_keys_on_machine: bool = nixops.util.attr_property(
        "storeKeysOnMachine", False, bool
    )
    keys: Dict[str, str] = nixops.util.attr_property("keys", {}, "json")
    owners: List[str] = nixops.util.attr_property("owners", [], "json")

    # Nix store path of the last global configuration deployed to this
    # machine.  Used to check whether this machine is up to date with
    # respect to the global configuration.
    cur_configs_path: Optional[str] = nixops.util.attr_property("configsPath", None)

    # Nix store path of the last machine configuration deployed to
    # this machine.
    cur_toplevel: Optional[str] = nixops.util.attr_property("toplevel", None)

    # Time (in Unix epoch) the instance was started, if known.
    start_time: Optional[int] = nixops.util.attr_property("startTime", None, int)

    # The value of the ‘system.stateVersion’ attribute at the time the
    # machine was created.
    state_version: Optional[str] = nixops.util.attr_property("stateVersion", None, str)

    def __init__(self, depl, name: str, id: int) -> None:
        nixops.resources.ResourceState.__init__(self, depl, name, id)
        self._ssh_pinged_this_time = False
        self.ssh = nixops.ssh_util.SSH(self.logger)
        self.ssh.register_flag_fun(self.get_ssh_flags)
        self.ssh.register_host_fun(self.get_ssh_name)
        self.ssh.register_passwd_fun(self.get_ssh_password)
        self._ssh_private_key_file: Optional[str] = None
        self.new_toplevel: Optional[str] = None

    def prefix_definition(self, attr):
        return attr

    @property
    def started(self) -> bool:
        state = self.state
        return state == self.STARTING or state == self.UP

    def set_common_state(self, defn) -> None:
        self.store_keys_on_machine = defn.store_keys_on_machine
        self.keys = defn.keys
        self.ssh_port = defn.ssh_port
        self.has_fast_connection = defn.has_fast_connection
        if not self.has_fast_connection:
            self.ssh.enable_compression()

    def stop(self) -> None:
        """Stop this machine, if possible."""
        self.warn("don't know how to stop machine ‘{0}’".format(self.name))

    def start(self) -> None:
        """Start this machine, if possible."""
        pass

    def get_load_avg(self) -> Union[List[str], None]:
        """Get the load averages on the machine."""
        try:
            res = (
                self.run_command("cat /proc/loadavg", capture_stdout=True, timeout=15)
                .rstrip()
                .split(" ")
            )
            assert len(res) >= 3
            return res
        except nixops.ssh_util.SSHConnectionFailed:
            return None
        except nixops.ssh_util.SSHCommandFailed:
            return None

    # FIXME: Move this to ResourceState so that other kinds of
    # resources can be checked.
    def check(self):  # TODO -> CheckResult, but supertype ResourceState -> True
        """Check machine state."""
        res = CheckResult()
        self._check(res)
        return res

    def _check(self, res):  # TODO -> None but supertype ResourceState -> True
        avg = self.get_load_avg()
        if avg == None:
            if self.state == self.UP:
                self.state = self.UNREACHABLE
            res.is_reachable = False
        else:
            self.state = self.UP
            self.ssh_pinged = True
            self._ssh_pinged_this_time = True
            res.is_reachable = True
            res.load = avg

            # Get the systemd units that are in a failed state or in progress.
            out = self.run_command(
                "systemctl --all --full --no-legend", capture_stdout=True
            ).split("\n")
            res.failed_units = []
            res.in_progress_units = []
            for l in out:
                match = re.match("^([^ ]+) .* failed .*$", l)
                if match:
                    res.failed_units.append(match.group(1))

                # services that are in progress
                match = re.match("^([^ ]+) .* activating .*$", l)
                if match:
                    res.in_progress_units.append(match.group(1))

                # Currently in systemd, failed mounts enter the
                # "inactive" rather than "failed" state.  So check for
                # that.  Hack: ignore special filesystems like
                # /sys/kernel/config and /tmp. Systemd tries to mount these
                # even when they don't exist.
                match = re.match("^([^\.]+\.mount) .* inactive .*$", l)
                if (
                    match
                    and not match.group(1).startswith("sys-")
                    and not match.group(1).startswith("dev-")
                    and not match.group(1) == "tmp.mount"
                ):
                    res.failed_units.append(match.group(1))

                if match and match.group(1) == "tmp.mount":
                    try:
                        self.run_command(
                            "cat /etc/fstab | cut -d' ' -f 2 | grep '^/tmp$' &> /dev/null"
                        )
                    except:
                        continue
                    res.failed_units.append(match.group(1))

    def restore(self, defn, backup_id: Optional[str], devices: List[str] = []):
        """Restore persistent disks to a given backup, if possible."""
        self.warn(
            "don't know how to restore disks from backup for machine ‘{0}’".format(
                self.name
            )
        )

    def remove_backup(self, backup_id, keep_physical=False):
        """Remove a given backup of persistent disks, if possible."""
        self.warn(
            "don't know how to remove a backup for machine ‘{0}’".format(self.name)
        )

    def get_backups(self) -> Dict[str, Dict[str, Any]]:
        self.warn("don't know how to list backups for ‘{0}’".format(self.name))
        return {}

    def backup(self, defn, backup_id: str, devices: List[str] = []) -> None:
        """Make backup of persistent disks, if possible."""
        self.warn(
            "don't know how to make backup of disks for machine ‘{0}’".format(self.name)
        )

    def reboot(self, hard: bool = False) -> None:
        """Reboot this machine."""
        self.log("rebooting...")
        if self.state == self.RESCUE:
            # We're on non-NixOS here, so systemd might not be available.
            # The sleep is to prevent the reboot from causing the SSH
            # session to hang.
            reboot_command = "(sleep 2; reboot) &"
        else:
            reboot_command = "systemctl reboot"
        self.run_command(reboot_command, check=False)
        self.state = self.STARTING
        self.ssh.reset()

    def reboot_sync(self, hard: bool = False) -> None:
        """Reboot this machine and wait until it's up again."""
        self.reboot(hard=hard)
        self.log_start("waiting for the machine to finish rebooting...")
        nixops.util.wait_for_tcp_port(
            self.get_ssh_name(),
            self.ssh_port,
            open=False,
            callback=lambda: self.log_continue("."),
        )
        self.log_continue("[down]")
        nixops.util.wait_for_tcp_port(
            self.get_ssh_name(), self.ssh_port, callback=lambda: self.log_continue(".")
        )
        self.log_end("[up]")
        self.state = self.UP
        self.ssh_pinged = True
        self._ssh_pinged_this_time = True
        self.send_keys()

    def reboot_rescue(self, hard: bool = False) -> None:
        """
        Reboot machine into rescue system and wait until it is active.
        """
        self.warn("machine ‘{0}’ doesn't have a rescue" " system.".format(self.name))

    def send_keys(self) -> None:
        if self.state == self.RESCUE:
            # Don't send keys when in RESCUE state, because we're most likely
            # bootstrapping plus we probably don't have /run mounted properly
            # so keys will probably end up being written to DISK instead of
            # into memory.
            return
        if self.store_keys_on_machine:
            return
        for k, opts in self.get_keys().items():
            self.log("uploading key ‘{0}’...".format(k))
            tmp = self.depl.tempdir + "/key-" + self.name
            if "destDir" not in opts:
                raise Exception("Key '{}' has no 'destDir' specified.".format(k))

            destDir = opts["destDir"].rstrip("/")
            self.run_command(
                (
                    "test -d '{0}' || ("
                    " mkdir -m 0750 -p '{0}' &&"
                    " chown root:keys  '{0}';)"
                ).format(destDir)
            )

            if "text" in opts:
                with open(tmp, "w+") as f:
                    f.write(opts['text'])
            elif 'keyCmd' in opts:
                with open(tmp, "w+") as f:
                    subprocess.Popen(opts['keyCmd'], stdout=f, shell=True)
            elif 'keyFile' in opts:
                self._logged_exec(["cp", opts['keyFile'], tmp])
            else:
                raise Exception(
                    "Neither 'text' or 'keyFile' options were set for key '{0}'.".format(
                        k
                    )
                )

            outfile = destDir + "/" + k
            # We scp to a temporary file and then mv because scp is not atomic.
            # See https://github.com/NixOS/nixops/issues/762
            tmp_outfile = destDir + "/." + k + ".tmp"
            outfile_esc = "'" + outfile.replace("'", r"'\''") + "'"
            tmp_outfile_esc = "'" + tmp_outfile.replace("'", r"'\''") + "'"
            self.run_command("rm -f " + outfile_esc + " " + tmp_outfile_esc)
            self.upload_file(tmp, tmp_outfile)
            # For permissions we use the temporary file as well, so that
            # the final outfile will appear atomically with the right permissions.
            self.run_command(
                " ".join(
                    [
                        # chown only if user and group exist,
                        # else leave root:root owned
                        "(",
                        " getent passwd '{1}' >/dev/null &&",
                        " getent group '{2}' >/dev/null &&",
                        " chown '{1}:{2}' {0}",
                        ");",
                        # chmod either way
                        "chmod '{3}' {0}",
                    ]
                ).format(
                    tmp_outfile_esc, opts["user"], opts["group"], opts["permissions"]
                )
            )
            self.run_command("mv " + tmp_outfile_esc + " " + outfile_esc)
            os.remove(tmp)
        self.run_command(
            "mkdir -m 0750 -p /run/keys && "
            "chown root:keys  /run/keys && "
            "touch /run/keys/done"
        )

    def get_keys(self):
        return self.keys

    def get_ssh_name(self):
        assert False

    def get_ssh_flags(self, scp=False):
        if scp:
            return ["-P", str(self.ssh_port)]
        else:
            return ["-p", str(self.ssh_port)]

    def get_ssh_password(self):
        return None

    def get_ssh_for_copy_closure(self):
        return self.ssh

    @property
    def public_host_key(self):
        return None

    @property
    def private_ipv4(self):
        return None

    def address_to(self, r):
        """Return the IP address to be used to access resource "r" from this machine."""
        return r.public_ipv4

    def wait_for_ssh(self, check=False):
        """Wait until the SSH port is open on this machine."""
        if self.ssh_pinged and (not check or self._ssh_pinged_this_time):
            return
        self.log_start("waiting for SSH...")
        nixops.util.wait_for_tcp_port(
            self.get_ssh_name(), self.ssh_port, callback=lambda: self.log_continue(".")
        )
        self.log_end("")
        if self.state != self.RESCUE:
            self.state = self.UP
        self.ssh_pinged = True
        self._ssh_pinged_this_time = True

    def write_ssh_private_key(self, private_key):
        key_file = "{0}/id_nixops-{1}".format(self.depl.tempdir, self.name)
        with os.fdopen(os.open(key_file, os.O_CREAT | os.O_WRONLY, 0o600), "w") as f:
            f.write(private_key)
        self._ssh_private_key_file = key_file
        return key_file

    def get_ssh_private_key_file(self):
        return None

    def _logged_exec(self, command, **kwargs):
        return nixops.util.logged_exec(command, self.logger, **kwargs)

    def run_command(self, command, **kwargs):
        """
        Execute a command on the machine via SSH.

        For possible keyword arguments, please have a look at
        nixops.ssh_util.SSH.run_command().
        """
        # If we are in rescue state, unset locale specific stuff, because we're
        # mainly operating in a chroot environment.
        if self.state == self.RESCUE:
            command = "export LANG= LC_ALL= LC_TIME=; " + command
        return self.ssh.run_command(command, self.get_ssh_flags(), **kwargs)

    def switch_to_configuration(
        self, method: str, sync: bool, command: Optional[str] = None
    ) -> int:
        """
        Execute the script to switch to new configuration.
        This function has to return an integer, which is the return value of the
        actual script.
        """
        cmd = "NIXOS_NO_SYNC=1 " if not sync else ""
        if command is None:
            cmd += "/nix/var/nix/profiles/system/bin/switch-to-configuration"
        else:
            cmd += command
        cmd += " " + method
        return self.run_command(cmd, check=False)

    def copy_closure_to(self, path):
        """Copy a closure to this machine."""

        # !!! Implement copying between cloud machines, as in the Perl
        # version.

        ssh = self.get_ssh_for_copy_closure()

        # Any remaining paths are copied from the local machine.
        env = dict(os.environ)
        env["NIX_SSHOPTS"] = " ".join(ssh._get_flags() + ssh.get_master().opts)
        self._logged_exec(
            ["nix-copy-closure", "--to", ssh._get_target(), path]
            + ([] if self.has_fast_connection else ["--use-substitutes"]),
            env=env,
        )

    def get_scp_name(self):
        ssh_name = self.get_ssh_name()
        # ipv6 addresses have to be wrapped in brackets for scp
        if ":" in ssh_name:
            return "[%s]" % (ssh_name)
        return ssh_name

    def upload_file(self, source, target, recursive=False):
        master = self.ssh.get_master()
        cmdline = ["scp"] + self.get_ssh_flags(True) + master.opts
        if recursive:
            cmdline += ["-r"]
        cmdline += [source, "root@" + self.get_scp_name() + ":" + target]
        return self._logged_exec(cmdline)

    def download_file(self, source, target, recursive=False):
        master = self.ssh.get_master()
        cmdline = ["scp"] + self.get_ssh_flags(True) + master.opts
        if recursive:
            cmdline += ["-r"]
        cmdline += ["root@" + self.get_scp_name() + ":" + source, target]
        return self._logged_exec(cmdline)

    def get_console_output(self):
        return "(not available for this machine type)\n"


class CheckResult(object):
    def __init__(self) -> None:
        # Whether the resource exists.
        self.exists = None

        # Whether the resource is "up".  Generally only meaningful for
        # machines.
        self.is_up = None

        # Whether the resource is reachable via SSH.
        self.is_reachable = None

        # Whether the disks that should be attached to a machine are
        # in fact properly attached.
        self.disks_ok = None

        # List of systemd units that are in a failed state.
        self.failed_units = None

        # List of systemd units that are in progress.
        self.in_progress_units = None

        # Load average on the machine.
        self.load = None

        # Error messages.
        self.messages: List[str] = []

        # FIXME: add a check whether the active NixOS config on the
        # machine is correct.
