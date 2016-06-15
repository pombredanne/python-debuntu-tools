# Debian and Ubuntu system administration tools.
#
# Author: Peter Odding <peter@peterodding.com>
# Last Change: June 15, 2016
# URL: https://debuntu-tools.readthedocs.io/

"""
Usage: debuntu-kernel-manager [OPTIONS] -- [APT_OPTIONS]

Detect and remove old Linux kernel header and image packages that can be safely
removed to conserve disk space and speed up apt-get runs that install or remove
kernels.

By default old packages are detected and reported on the command line but no
changes are made. To actually remove old packages you need to use the -c,
--clean or --remove option. Using the following command you can perform
a dry run that shows you what will happen without actually doing it:

  $ debuntu-kernel-manager --remove -- --dry-run

The debuntu-kernel-manager program is currently in alpha status, which means
a first attempt at a usable program has been published but there are no
guarantees about what it actually does. You have been warned :-).

Supported options:

  -c, --clean, --remove

   Remove Linux kernel header and/or image packages that are deemed to be safe
   to remove. The use of this option requires sudo access on the system in
   order to run the 'apt-get remove' command.

  -f, --force

    When more than one Linux kernel meta package is installed the -c, --clean
    and --remove options will refuse to run apt-get and exit with an error
    instead. Use the -f or --force option to override this sanity check.

  -r, --remote-host=ALIAS

    Detect and remove old Linux kernel header and image packages on a remote
    host over SSH. The ALIAS argument gives the SSH alias that should be used
    to connect to the remote host.

  -v, --verbose

    Increase verbosity (can be repeated).

  -q, --quiet

    Decrease verbosity (can be repeated).

  -h, --help

    Show this message and exit.
"""

# Standard library modules.
import collections
import getopt
import sys

# External dependencies.
import coloredlogs
from deb_pkg_tools.version import Version
from executor import ExternalCommandFailed
from executor.contexts import create_context
from humanfriendly import AutomaticSpinner, Timer, compact, concatenate, pluralize
from humanfriendly.terminal import ansi_wrap, usage, warning
from property_manager import PropertyManager, cached_property, key_property, required_property
from verboselogs import VerboseLogger

# Initialize a logger for this module.
logger = VerboseLogger(__name__)


def main():
    """Command line interface for ``debuntu-kernel-manager``."""
    # Initialize logging to the terminal and system log.
    coloredlogs.install(syslog=True)
    # Parse the command line arguments.
    context_opts = dict()
    collector_opts = dict()
    action = 'render_summary'
    try:
        options, arguments = getopt.getopt(sys.argv[1:], 'cfr:vqh', [
            'clean', 'remove',
            'force', 'remote-host=',
            'verbose', 'quiet', 'help',
        ])
        for option, value in options:
            if option in ('-c', '--clean', '--remove'):
                action = 'cleanup_packages'
            elif option in ('-f', '--force'):
                collector_opts['force'] = True
            elif option in ('-r', '--remote-host'):
                context_opts['ssh_alias'] = value
            elif option in ('-v', '--verbose'):
                coloredlogs.increase_verbosity()
            elif option in ('-q', '--quiet'):
                coloredlogs.decrease_verbosity()
            elif option in ('-h', '--help'):
                usage(__doc__)
                return
            else:
                raise Exception("Unhandled option!")
        # Any positional arguments are passed to apt-get.
        collector_opts['apt_options'] = arguments
    except Exception as e:
        warning("Failed to parse command line arguments! (%s)", e)
        sys.exit(1)
    # Execute the requested action(s).
    context = create_context(**context_opts)
    try:
        collector = KernelCollector(
            context=context,
            **collector_opts
        )
        getattr(collector, action)()
    except (CleanupError, ExternalCommandFailed) as e:
        logger.error("%s", e)
        sys.exit(1)
    except Exception:
        logger.exception("Encountered unexpected exception on %s!", context)
        sys.exit(1)


class KernelCollector(PropertyManager):

    """Python API for automated Linux kernel image package cleanup on Debian based systems."""

    @required_property
    def apt_options(self):
        """A list of strings with command line options to pass to ``apt-get``."""
        return []

    @required_property(repr=False)
    def context(self):
        """An execution context created by :mod:`executor.contexts`."""

    @required_property
    def force(self):
        """Whether to continue with removal despite warnings (a boolean, defaults to :data:`False`)."""
        return False

    @required_property
    def preserve_count(self):
        """The number of kernel packages to preserve (an integer, defaults to 2)."""
        return 2

    @cached_property
    def installed_packages(self):
        """
        A dictionary that maps package names (strings) to :class:`MaybeKernelPackage` objects.

        The value of this property is generated by parsing the output of the
        ``dpkg --list`` command.
        """
        mapping = {}
        output = self.context.capture('dpkg', '--list')
        for line in output.splitlines():
            tokens = line.split()
            if len(tokens) >= 3:
                status = tokens[0]
                if len(status) == 2 and status.isalnum():
                    name = tokens[1]
                    mapping[name] = MaybeKernelPackage(
                        name=name,
                        version=Version(tokens[2]),
                        status=status,
                    )
        return mapping

    @cached_property
    def installed_header_packages(self):
        """A list of :class:`MaybeKernelPackage` objects for the installed Linux kernel header packages."""
        return sorted((package for package in self.installed_packages.values()
                       if package.status in ('ii', 'rc') and package.is_header_package),
                      key=lambda package: (package.version, package.name))

    @cached_property
    def installed_kernel_packages(self):
        """A list of :class:`MaybeKernelPackage` objects for the installed kernel images."""
        return sorted((package for package in self.installed_packages.values()
                       if package.status in ('ii', 'rc') and package.is_kernel_package),
                      key=lambda package: (package.version, package.name))

    @cached_property
    def installed_header_meta_packages(self):
        """A list of :class:`MaybeKernelPackage` objects with installed meta packages for kernel headers."""
        # We change the sort key for meta packages from (name, version) to just
        # the version so that the meta packages are listed in version order
        # despite the names being different.
        return sorted((package for package in self.installed_packages.values()
                       if package.status in ('ii', 'rc') and package.is_header_meta_package),
                      key=lambda p: p.version)

    @cached_property
    def installed_image_meta_packages(self):
        """A list of :class:`MaybeKernelPackage` objects with installed meta packages for kernel images."""
        # We change the sort key for meta packages from (name, version) to just
        # the version so that the meta packages are listed in version order
        # despite the names being different.
        return sorted((package for package in self.installed_packages.values()
                       if package.status in ('ii', 'rc') and package.is_image_meta_package),
                      key=lambda p: p.version)

    @cached_property
    def installed_package_groups(self):
        """A list of sets with :class:`MaybeKernelPackage` objects for installed header and kernel packages."""
        grouped_packages = collections.defaultdict(list)
        for package in self.installed_packages.values():
            if package.status in ('ii', 'rc') and package.is_kernel_or_header_package:
                grouped_packages[package.version_in_name].append(package)
        return sorted(grouped_packages.values(), key=lambda group: group[0].version)

    @cached_property
    def active_kernel_package(self):
        """The package name for the running kernel (a string)."""
        return 'linux-image-%s' % self.context.capture('uname', '--kernel-release')

    @cached_property
    def removable_package_groups(self):
        """
        A list of sets with :class:`MaybeKernelPackage` objects considered to be removable.

        Candidates for removal are selected from :attr:`installed_package_groups`,
        ignoring :attr:`active_kernel_package` and the newest
        :attr:`preserve_count` kernel images (minus one when
        :attr:`active_kernel_package` was ignored).
        """
        may_remove = []
        will_remove = []
        preserve_count = self.preserve_count
        for group in self.installed_package_groups:
            if any(package.name == self.active_kernel_package for package in group):
                # Never remove the package group for the active kernel image.
                preserve_count -= 1
            elif not any(package.is_kernel_package for package in group):
                # Ignore package groups that don't contain a kernel image.
                will_remove.append(group)
            else:
                # Consider removing nonactive package groups with a kernel image.
                may_remove.append(group)
        # Prefer new kernels over old kernels and respect
        # the configured number of packages to preserve.
        return will_remove + may_remove[:-preserve_count]

    @cached_property
    def removable_header_packages(self):
        """A list of :class:`MaybeKernelPackage` objects for header packages considered to be removable."""
        return sorted(pkg for grp in self.removable_package_groups for pkg in grp if pkg.is_header_package)

    @cached_property
    def removable_kernel_packages(self):
        """A list of :class:`MaybeKernelPackage` objects for kernel packages considered to be removable."""
        return sorted(pkg for grp in self.removable_package_groups for pkg in grp if pkg.is_kernel_package)

    @cached_property
    def removable_packages(self):
        """A list of :class:`MaybeKernelPackage` objects for kernel related packages considered to be removable."""
        return self.removable_header_packages + self.removable_kernel_packages

    @cached_property
    def cleanup_command(self):
        """A list of strings with the ``apt-get`` command to remove old packages."""
        command_line = []
        if self.removable_packages:
            command_line.extend(['apt-get', 'remove', '--purge'])
            command_line.extend(self.apt_options)
            for group in self.removable_package_groups:
                command_line.extend(package.name for package in group)
        return command_line

    def render_summary(self):
        """Render a summary of installed and removable kernel packages on the terminal."""
        logger.verbose("Sanity checking meta packages on %s ..", self.context)
        with AutomaticSpinner(label="Gathering information about %s" % self.context):
            # Report the installed Linux kernel header/image meta package(s).
            meta_package_types = (
                    (self.installed_image_meta_packages, "image", True),
                    (self.installed_header_meta_packages, "header", False),
            )
            for collection, label, expected in meta_package_types:
                if collection:
                    detailed_label = pluralize(len(collection), "installed Linux kernel %s meta package" % label)
                    logger.info("Found %s:", detailed_label)
                    for package in collection:
                        logger.info(" - %s (%s)", package.name, package.version)
                    if len(collection) > 1:
                        logger.warning(compact("""
                            You have more than one Linux kernel {label} meta
                            package installed ({packages}) which means
                            automatic package removal can be unreliable!
                        """, label=label, packages=concatenate(package.name for package in collection)))
                        logger.verbose(compact("""
                            It's probably best to stick to one Linux kernel
                            {label} meta package, preferably the one that
                            matches the newest kernel :-)
                        """, label=label))
                elif expected:
                    logger.warning("It looks like there's no Linux kernel %s package installed!", label)
                    logger.info("I hope you've thought about how to handle security updates?")
            # Report the installed Linux kernel header/image package(s).
            logger.verbose("Checking for removable packages on %s ..", self.context)
            package_types = (
                    (self.installed_kernel_packages, "image", True),
                    (self.installed_header_packages, "header", False),
            )
            for collection, label, expected in package_types:
                if collection:
                    logger.info("Found %s:", pluralize(len(collection), "installed Linux kernel %s package" % label))
                    for group in self.installed_package_groups:
                        matching_packages = sorted(package.name for package in group if package in collection)
                        active_group = any(package.name == self.active_kernel_package for package in group)
                        removable_group = (group in self.removable_package_groups)
                        if matching_packages:
                            logger.info(
                                " - %s (%s)",
                                concatenate(matching_packages),
                                ansi_wrap("removable", color='green')
                                if removable_group else ansi_wrap(
                                    "the active kernel" if active_group else
                                    ("one of %i newest kernels" % self.preserve_count),
                                    color='blue'
                                ),
                            )
                elif expected:
                    logger.warning("No installed %s packages found, this can't be right?!", label)
            # Report the removable packages.
            if self.removable_packages:
                logger.info(
                    "Found %s that can be removed.",
                    pluralize(len(self.removable_packages), "package"),
                )
                # Report the shell command to remove the packages.
                logger.verbose("Command to remove packages: %s", ' '.join(self.cleanup_command))
            else:
                logger.info("No packages need to be removed! :-)")

    def cleanup_packages(self, **options):
        """
        Run ``apt-get`` to cleanup removable kernel related packages.

        :param options: Any keyword arguments are passed on to the
                        :func:`~executor.contexts.AbstractContext.execute()`
                        method of the :class:`context` object.
        :raises: :exc:`CleanupError` when multiple Linux kernel meta packages
                 are installed and :attr:`force` is :data:`False`.
        """
        timer = Timer()
        self.render_summary()
        if self.cleanup_command:
            if len(self.installed_image_meta_packages) > 1 and not self.force:
                raise CleanupError(compact("""
                    Refusing to cleanup kernel related packages on {system}
                    because results can be unreliable when multiple Linux
                    kernel meta packages are installed! You can use the -f,
                    --force option to override this sanity check.
                """, system=self.context))
            logger.info("Removing %s on %s ..", pluralize(len(self.removable_packages), "package"), self.context)
            self.context.execute(*self.cleanup_command, sudo=True, **options)
            logger.info("Done! (took %s)", timer)


class MaybeKernelPackage(PropertyManager):

    """Dumb container for entries parsed from ``dpkg --list`` output."""

    # Explicitly define the sort order of the key properties.
    key_properties = 'name', 'version'

    @key_property
    def name(self):
        """The name of the package (a string)."""

    @key_property
    def version(self):
        """The version of the package (a :class:`~deb_pkg_tools.version.Version` object)."""

    @required_property
    def status(self):
        """The status of the package (a string)."""

    @cached_property
    def is_header_meta_package(self):
        """:data:`True` if the package is a Linux kernel header meta package, :data:`False` otherwise."""
        tokens = self.name.split('-')
        return (len(tokens) >= 3 and
                tokens[0] == 'linux' and
                'headers' in tokens and
                not any(t[0].isdigit() for t in tokens))

    @cached_property
    def is_image_meta_package(self):
        """:data:`True` if the package is a Linux kernel image meta package, :data:`False` otherwise."""
        tokens = self.name.split('-')
        return (len(tokens) >= 3 and
                tokens[0] == 'linux' and
                'image' in tokens and
                not any(t[0].isdigit() for t in tokens))

    @cached_property
    def is_header_package(self):
        """:data:`True` if the package is a specific version of the Linux kernel headers, :data:`False` otherwise."""
        tokens = self.name.split('-')
        return (len(tokens) >= 3 and
                tokens[0] == 'linux' and
                tokens[1] == 'headers' and
                tokens[2][0].isdigit())

    @cached_property
    def is_kernel_package(self):
        """:data:`True` if the package is a specific version of the Linux kernel image, :data:`False` otherwise."""
        tokens = self.name.split('-')
        return (tokens and tokens[0] == 'linux' and
                ((len(tokens) >= 3 and tokens[1] == 'image' and tokens[2][0].isdigit()) or
                 (len(tokens) >= 4 and tokens[1] == 'image' and tokens[2] == 'extra' and tokens[3][0].isdigit()) or
                 (len(tokens) >= 4 and tokens[1] == 'signed' and tokens[2] == 'image' and tokens[3][0].isdigit())))

    @cached_property
    def is_kernel_or_header_package(self):
        """:data:`True` if the package is a Linux kernel image or headers package, :data:`False` otherwise."""
        return self.is_header_package or self.is_kernel_package

    @cached_property
    def version_in_name(self):
        """The version encoded in the name of the package (a string or :data:`None`)."""
        if self.is_kernel_or_header_package:
            tokens = self.name.split('-')
            # Remove the static prefix from the package name.
            for prefix in 'linux', 'signed', 'image', 'extra', 'headers':
                if tokens and tokens[0] == prefix:
                    tokens.pop(0)
            # Ignore the kernel type that may be encoded in the package name.
            if tokens[-1].isalpha():
                tokens.pop(-1)
            # Reconstruct the version number encoded in the name of the package.
            return '-'.join(tokens)

    @cached_property
    def kernel_type(self):
        """The kernel type encoded in the name of the package (a string or :data:`None`)."""
        if self.is_kernel_or_header_package:
            tokens = self.name.split('-')
            if tokens[-1].isalpha():
                return tokens[-1]


class CleanupError(Exception):

    """
    Custom exception to detect known problems.

    Raised by :class:`~KernelCollector` when multiple Linux kernel meta packages
    are installed but :attr:`~.KernelCollector.force` is :data:`False`.
    """


if __name__ == '__main__':
    main()