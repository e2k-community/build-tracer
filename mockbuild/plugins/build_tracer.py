# -*- coding: utf-8 -*-
# vim:expandtab:autoindent:tabstop=4:shiftwidth=4:filetype=python:textwidth=0:
# License: GPL2 or later see COPYING
# Copyright (c) 2025, LLC NIC CT
# Copyright (c) 2025, Vladislav Shchapov <vladislav@shchapov.ru>

# python library imports
import os.path

# our imports
from mockbuild.mounts import BindMountPoint
from mockbuild.trace_decorator import getLog, traceLog
from mockbuild import util, file_util

requires_api_version = "1.1"


# plugin entry point
@traceLog()
def init(plugins, conf, buildroot):
    BuildTracer(plugins, conf, buildroot)


class BuildTracer(object):
    # pylint: disable=too-few-public-methods
    """Make build trace"""
    @traceLog()
    def __init__(self, plugins, conf, buildroot):
        self.buildroot = buildroot
        self.config = buildroot.config
        self.opts = conf

        self.trace_rpmbuild_command = '/usr/bin/build-tracer-rpmbuild.py'
        self.host_trace_rpmbuild_command = self.opts.get('host_trace_rpmbuild_command', self.trace_rpmbuild_command)
        self.output_dir_name = self.opts.get('dir_name', 'build_trace')

        # Установка зависимостей в chroot
        def list_extend_nodup(lst, b):
            lst.extend([v for v in b if v not in lst])
        list_extend_nodup(self.config['chroot_additional_packages'], ['python3', 'strace'])

        # actually run our plugin at this step
        plugins.add_hook("preinit", self._PreInitHook)
        plugins.add_hook("postbuild", self._PostBuildHook)

        # Монтирование трассирующего rpmbuild из хост-системы.
        buildroot.mounts.add(
            BindMountPoint(
                srcpath=self.host_trace_rpmbuild_command,
                bindpath=buildroot.make_chroot_path(self.trace_rpmbuild_command)
            )
        )

        getLog().info("BuildTracer: initialized")


    # =============
    # 'Private' API
    # =============

    # Настройка окружения.
    @traceLog()
    def _PreInitHook(self):
        getLog().info("BuildTracer: enabled")
        output_dir = os.path.join("/builddir", self.output_dir_name)
        envupd = {
            "BUILD_TRACER_OUTPUT_DIR": output_dir,
            "BUILD_TRACER_RPMBUILD_COMMAND": self.config["rpmbuild_command"],
        }
        if strace_command := self.opts.get('strace_command'):
            envupd["BUILD_TRACER_STRACE_COMMAND"] = strace_command
        self.buildroot.env.update(envupd)
        file_util.mkdirIfAbsent(self.buildroot.make_chroot_path(output_dir))
        self.config.update({'rpmbuild_command': self.trace_rpmbuild_command})

    # Извлечение данных трассировки и упаковка их в архив.
    @traceLog()
    def _PostBuildHook(self):
        tarfile = os.path.join(self.buildroot.resultdir, self.output_dir_name + ".tar.gz")
        getLog().info("BuildTracer: creating tarball %s", tarfile)
        tar_cmd = self.config["tar_binary"]
        util.do(
            [tar_cmd, "--owner=root", "--group=root", "-czf", tarfile, self.output_dir_name],
            cwd=self.buildroot.make_chroot_path('/builddir'), shell=False, printOutput=True,
        )
