"""short doc

long doc

"""
import sys
import os
from pathlib import Path
import pwd
import platform
import locale
import subprocess
import json
import re
import config  # config.Config.VERSION  Refactor after src-layout migration
import tools


def collect_diagnostics():
    """Collect information about environment and versions of tools and
    packages used by Back In Time.

    The information can be used e.g. for debugging and bug reports.

    Returns:
       dict: A nested dictionary.
    """
    result = {}

    pwd_struct = pwd.getpwuid(os.getuid())

    # === BACK IN TIME ===

    # work-around: Instantiate to get the user-callback folder
    # (should be singleton)
    cfg = config.Config()

    result['backintime'] = {
        'name': config.Config.APP_NAME,
        'version': config.Config.VERSION,
        'latest-config-version': config.Config.CONFIG_VERSION,
        'local-config-file': cfg._LOCAL_CONFIG_PATH,
        'local-config-file-found': Path(cfg._LOCAL_CONFIG_PATH).exists(),
        'global-config-file': cfg._GLOBAL_CONFIG_PATH,
        'global-config-file-found': Path(cfg._GLOBAL_CONFIG_PATH).exists(),
        # 'distribution-package': str(distro_path),
        'started-from': str(Path(config.__file__).parent),
        'running-as-root': pwd_struct.pw_name == 'root',
        'user-callback': cfg.takeSnapshotUserCallback(),
        'keyring-supported': tools.keyringSupported()
    }

    # Git repo
    bit_root_path = Path(tools.backintimePath(""))
    git_info = get_git_repository_info(bit_root_path)

    if git_info:

        result['backintime']['git-project-root'] = str(bit_root_path)

        for key in git_info:
            result['backintime'][f'git-{key}'] = git_info[key]

    # == HOST setup ===
    result['host-setup'] = {
        # Kernel & Architecture
        'platform': platform.platform(),
        # OS Version (and maybe name)
        'system': '{} {}'.format(platform.system(), platform.version()),
        # OS Release name (prettier)
        'os-release': _get_os_release()

    }

    # Display system (X11 or Wayland)
    # This doesn't catch all edge cases.
    # For more detials see: https://unix.stackexchange.com/q/202891/136851
    result['host-setup']['display-system'] = os.environ.get(
        'XDG_SESSION_TYPE', '($XDG_SESSION_TYPE not set)')

    # locale (system language etc)
    #
    # Implementation note: With env var "LC_ALL=C" getlocale() will return (None, None).
    # This throws an error in "join()":
    #   TypeError: sequence item 0: expected str instance, NoneType found
    my_locale = locale.getlocale()
    if all(x is None for x in my_locale):
        my_locale = ["(Unknown)"]
    result['host-setup']['locale'] = ', '.join(my_locale)

    # PATH environment variable
    result['host-setup']['PATH'] = os.environ.get('PATH', '($PATH unknown)')

    # RSYNC environment variables
    for var in ['RSYNC_OLD_ARGS', 'RSYNC_PROTECT_ARGS']:
        result['host-setup'][var] = os.environ.get(var, '(not set)')

    # === PYTHON setup ===
    python = '{} {} {} {}'.format(
        platform.python_version(),
        ' '.join(platform.python_build()),
        platform.python_implementation(),
        platform.python_compiler()
    )

    # Python branch and revision if available
    branch = platform.python_branch()
    if branch:
        python = '{} branch: {}'.format(python, branch)
    rev = platform.python_revision()
    if rev:
        python = '{} rev: {}'.format(python, rev)

    python_executable = Path(sys.executable)

    # Python interpreter
    result['python-setup'] = {
        'python': python,
        'python-executable': str(python_executable),
        'python-executable-symlink': python_executable.is_symlink(),
    }

    # Real interpreter path if it is used via a symlink
    if result['python-setup']['python-executable-symlink']:
        result['python-setup']['python-executable-resolved'] \
            = str(python_executable.resolve())

    result['python-setup']['sys.path'] = sys.path

    # Qt
    try:
        import PyQt5.QtCore
    except ImportError:
        qt = '(Can not import PyQt5)'
    else:
        qt = 'PyQt {} / Qt {}'.format(PyQt5.QtCore.PYQT_VERSION_STR,
                                      PyQt5.QtCore.QT_VERSION_STR)
    finally:
        result['python-setup']['qt'] = qt

    # === EXTERN TOOL ===
    result['external-programs'] = {}

    # rsync
    # rsync >= 3.2.6: -VV return a json
    # rsync <= 3.2.5 and > (somewhere near) 3.1.3: -VV return the same as -V
    # rsync <= (somewhere near) 3.1.3: -VV doesn't exists
    # rsync == 3.1.3 (Ubuntu 20 LTS) doesn't even know '-V'

    # This works when rsync understands -VV and returns json or human readable
    result['external-programs']['rsync'] = _get_extern_versions(
        ['rsync', '-VV'],
        r'rsync  version (.*)  protocol version',
        try_json=True,
        error_pattern=r'unknown option'
    )

    # When -VV was unknown use -V and parse the human readable output
    if not result['external-programs']['rsync']:
        # try the old way
        result['external-programs']['rsync'] = _get_extern_versions(
            ['rsync', '--version'],
            r'rsync  version (.*)  protocol version'
        )

    # ssh
    result['external-programs']['ssh'] = _get_extern_versions(['ssh', '-V'])

    # sshfs
    result['external-programs']['sshfs'] \
        = _get_extern_versions(['sshfs', '-V'], r'SSHFS version (.*)\n')

    # EncFS
    # Using "[Vv]" in the pattern because encfs does translate its output.
    # e.g. In German it is "Version" in English "version".
    result['external-programs']['encfs'] \
        = _get_extern_versions(['encfs'], r'Build: encfs [Vv]ersion (.*)\n')

    # Shell
    SHELL_ERR_MSG = '($SHELL not exists)'
    shell = os.environ.get('SHELL', SHELL_ERR_MSG)
    result['external-programs']['shell'] = shell

    if shell != SHELL_ERR_MSG:
        shell_version = _get_extern_versions([shell, '--version'])
        result['external-programs']['shell-version'] \
            = shell_version.split('\n')[0]

    result = _replace_username_paths(result=result,
                                     username=pwd_struct.pw_name)

    return result


def _get_extern_versions(cmd,
                         pattern=None,
                         try_json=False,
                         error_pattern=None):
    """Get the version of an external tools using ``subprocess.Popen()``.

     Args:
         cmd (list): Commandline arguments that will be passed to `Popen()`.
         pattern (str): A regex pattern to extract the version string from the
                        commands output.
         try_json (bool): Interpret the output as json first (default: False).
                          If it could be parsed the result is a dict
         error_pattern (str): Regex pattern to identify a message in the output
                              that indicates an error.

     Returns:
         Version information as string (or dict if was JSON and parsed successfully).
         `None` if the error_pattern did match (to indicate an error)
     """

    try:
        # as context manager to prevent ResourceWarning's
        with subprocess.Popen(cmd,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              universal_newlines=True) as proc:
            error_output = proc.stderr.read()
            std_output = proc.stdout.read()

    except FileNotFoundError:
        result = f'(no {cmd[0]})'

    else:
        # Check for errors
        if error_pattern:
            match = re.findall(error_pattern, error_output)

            if match:
                return None

        # some tools use "stderr" for version info
        if not std_output:
            result = error_output
        else:
            result = std_output

        # Expect JSON string
        if try_json:

            try:
                result = json.loads(result)

            except json.decoder.JSONDecodeError:
                # Wasn't a json. Try regex in the next block.
                pass

            else:
                return result  # as JSON

        # extract version string
        if pattern:
            result = re.findall(pattern, result)[0]

    return result.strip()  # as string


def get_git_repository_info(path=None):
    """Return the current branch and last commit hash.

    Credits: https://stackoverflow.com/a/51224861/4865723

    Args:
        path (Path): Path with '.git' folder in (default is
                     current working directory).

    Returns:
        (dict): Dict with keys "branch" and "hash" if it is a git repo,
                otherwise an `None`.
    """

    if not path:
        path = Path.cwd()

    git_folder = path / '.git'

    if not git_folder.exists():
        return None

    result = {}

    # branch name
    with (git_folder / 'HEAD').open('r') as handle:
        val = handle.read()

    if val.startswith('ref: '):
        result['branch'] = '/'.join(val.split('/')[2:]).strip()

    else:
        result['branch'] = '(detached HEAD)'
        result['hash'] = val

        return result

    # commit hash
    with (git_folder / 'refs' / 'heads' / result['branch']) \
         .open('r') as handle:
        result['hash'] = handle.read().strip()

    return result


def _get_os_release():
    """Extract infos from os-release file.

    Returns:
        (str): OS name e.g. "Debian GNU/Linux 11 (bullseye)"
    """

    try:
        # content of /etc/os-release
        return platform.freedesktop_os_release()  # since Python 3.10
    except AttributeError:  # refactor: when we drop Python 3.9 support
        pass

    # read and parse the os-release file ourself
    fp = Path('/etc') / 'os-release'

    try:
        with fp.open('r') as handle:
            osrelease = handle.read()
    except FileNotFoundError:
        return '(os-release file not found)'

    return re.findall('PRETTY_NAME=\"(.*)\"', osrelease)[0]



def _replace_username_paths(result, username):
    """User's homepath and the username is replaced because of security
    reasons.

    Args:
        result (dict): Dict possibily containing the username and its home
                       path.
        username (str). The user login name to look for.

    Returns:
        (str): String with replacements.
    """

    # Replace home folder user names with this dummy name
    # for privacy reasons
    USER_REPLACED = 'UsernameReplaced'

    # JSON to string
    result = json.dumps(result)

    result = result.replace(f'/home/{username}', f'/home/{USER_REPLACED}')
    result = result.replace(f'~/{username}', f'~/{USER_REPLACED}')

    # string to JSON
    return json.loads(result)
