"""
Copyright 2017 Oliver Smith

This file is part of pmbootstrap.

pmbootstrap is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

pmbootstrap is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with pmbootstrap.  If not, see <http://www.gnu.org/licenses/>.
"""
import os
import logging

import pmb.build
import pmb.build.autodetect
import pmb.build.buildinfo
import pmb.chroot
import pmb.chroot.apk
import pmb.chroot.distccd
import pmb.parse
import pmb.parse.arch


def get_apkbuild(args, pkgname, arch):
    """
    Find the APKBUILD path for pkgname. When there is none, try to find it in
    the binary package APKINDEX files or raise an exception.

    :param pkgname: package name to be built, as specified in the APKBUILD
    :returns: None or full path to APKBUILD
    """
    aport = pmb.build.find_aport(args, pkgname, False)
    if aport:
        return pmb.parse.apkbuild(args, aport + "/APKBUILD")
    if pmb.parse.apkindex.read_any_index(args, pkgname, arch):
        return None
    raise RuntimeError("Package '" + pkgname + "': Could not find aport, and"
                       " could not find this package in any APKINDEX!")


def build_dependencies(args, apkbuild, arch, strict, force, suffix):
    # Is the build necessary? (before installing dependencies)
    is_necessary_pre_depends = pmb.build.is_necessary(args, arch, apkbuild)
    if not force and not is_necessary_pre_depends:
        return True

    # Build/install dependencies
    if strict:
        for depend in apkbuild["makedepends"] + apkbuild["depends"]:
            package(args, depend, arch, strict=True)
    elif len(apkbuild["makedepends"]):
        pmb.chroot.apk.install(args, apkbuild["makedepends"], suffix)

    # Avoid re-building for circular dependencies
    if not pmb.build.is_necessary(args, arch, apkbuild):
        if force and not is_necessary_pre_depends:
            return True


def init_buildenv(args, apkbuild, arch, strict, force, cross, suffix):
    # Build and install dependencies (don't rebuild circular depends)
    if build_dependencies(args, apkbuild, arch, strict, force, suffix):
        return True

    # Install and configure abuild and gcc
    pmb.build.init(args, suffix)
    pmb.build.other.configure_abuild(args, suffix)

    # Cross-compiler init
    if cross:
        pmb.chroot.apk.install(args, ["gcc-" + arch, "g++-" + arch,
                                      "ccache-cross-symlinks"])
    if cross == "distcc":
        pmb.chroot.apk.install(args, ["distcc"], suffix=suffix,
                               build=False)
        pmb.chroot.distccd.start(args, arch)


def run_abuild(args, apkbuild, arch, strict, force, cross, suffix):
    # Sanity check
    if cross == "native" and "!tracedeps" not in apkbuild["options"]:
        logging.info("WARNING: Option !tracedeps is not set, but we're"
                     " cross-compiling in the native chroot. This will"
                     " probably fail!")

    # Pretty log message
    output = (arch + "/" + apkbuild["pkgname"] + "-" + apkbuild["pkgver"] +
              "-r" + apkbuild["pkgrel"] + ".apk")
    logging.info("(" + suffix + ") build " + output)

    # Environment variables
    env = {"CARCH": arch}
    if cross == "native":
        hostspec = pmb.parse.arch.alpine_to_hostspec(arch)
        env["CROSS_COMPILE"] = hostspec + "-"
        env["CC"] = hostspec + "-gcc"
    if cross == "distcc":
        env["PATH"] = "/usr/lib/distcc/bin:" + pmb.config.chroot_path
        env["DISTCC_HOSTS"] = "127.0.0.1:" + args.port_distccd

    # Build the abuild command
    cmd = []
    for key, value in env.items():
        cmd += [key + "=" + value]
    cmd += ["abuild"]
    if strict:
        cmd += ["-r"]  # install depends with abuild
    else:
        cmd += ["-d"]  # do not install depends with abuild
    if force:
        cmd += ["-f"]

    # Copy the aport to the chroot and build it
    pmb.build.copy_to_buildpath(args, apkbuild["pkgname"], suffix)
    pmb.chroot.user(args, cmd, suffix, "/home/pmos/build")
    return output


def clean_up(args, apkbuild, arch, buildinfo, strict, suffix, output):
    # Verify output file
    path = args.work + "/packages/" + output
    if not os.path.exists(path):
        raise RuntimeError("Package not found after build: " + path)

    # Create .buildinfo.json file
    if buildinfo:
        logging.info("(" + suffix + ") generate " + output + ".buildinfo.json")
        pmb.build.buildinfo.write(args, output, arch, suffix, apkbuild)

    # Symlink noarch package (and subpackages)
    if "noarch" in apkbuild["arch"]:
        pmb.build.symlink_noarch_packages(args)

    # Clean up (APKINDEX cache, depends when strict)
    pmb.parse.apkindex.clear_cache(args, args.work + "/packages/" +
                                   arch + "/APKINDEX.tar.gz")
    if strict:
        logging.info("(" + suffix + ") uninstall makedepends")
        pmb.chroot.user(args, ["abuild", "undeps"], suffix, "/home/pmos/build")


def package(args, pkgname, arch, force=False, buildinfo=False, strict=False):
    """
    Build a package with Alpine Linux' abuild.

    :param pkgname: package name to be built, as specified in the APKBUILD
    :param arch: architecture we're building for
    :param force: even build, if not necessary
    :param buildinfo: record the build environment in a .buildinfo.json file
    :param strict: avoid building with irrelevant dependencies installed by
                   letting abuild install and uninstall all dependencies. It
                   also installs the depends, and not only the makedepends like
                   pmbootstrap does normally to improve performance.
    :returns: output path relative to the packages folder
    """
    # Only build when APKBUILD exists
    apkbuild = get_apkbuild(args, pkgname, arch)
    if not apkbuild:
        return

    # Set up the build environment (don't rebuild circular depends)
    arch = pmb.build.autodetect.arch(args, apkbuild, arch, strict)
    suffix = pmb.build.autodetect.suffix(args, apkbuild, arch)
    cross = pmb.build.autodetect.crosscompile(args, apkbuild, arch, suffix)
    if init_buildenv(args, apkbuild, arch, strict, force, cross, suffix):
        return

    # Build and clean up
    output = run_abuild(args, apkbuild, arch, strict, force, cross, suffix)
    clean_up(args, apkbuild, arch, buildinfo, strict, suffix, output)
    return output
