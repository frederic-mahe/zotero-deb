#!/usr/bin/env python3

import os, sys
import configparser
import types
import shutil, shlex
import subprocess
import tempfile
import argparse
import re
import filetype
import magic

args = argparse.ArgumentParser(description='update Zotero deb repo.')
args.add_argument('--base', type=str, default='.')
args.add_argument('--config', type=str)
args.add_argument('--mime', type=str)
args.add_argument('source', nargs='+')
args = args.parse_args()

class IniFile():
  def __init__(self, path):
    self.ini = configparser.RawConfigParser()
    self.ini.optionxform=str
    self.ini.read(path)

  def __enter__(self):
    return self.ini

  def __exit__(self, exc_type, exc_value, exc_traceback):
    pass

config = types.SimpleNamespace()
config.base = os.path.abspath(args.base)
config.mime = args.mime or 'mime.xml'

# load build config
with IniFile(args.config or 'config.ini') as ini:
  config.ini = ini
config.maintainer = config.ini['maintainer']['email']
config.gpgkey = config.ini['maintainer']['gpgkey']

# remove trailing slash since it messes with basename
config.source = [ re.sub(r'/$', '', source) for source in args.source ]

# make repo directory
config.repo = os.path.join(config.base, 'release/apt')
os.makedirs(config.repo, exist_ok=True)

for source in config.source:
  assert os.path.isdir(source)

  deb = types.SimpleNamespace()

  arch = magic.from_file(os.path.join(source, 'zotero-bin'))
  if arch.startswith('ELF 32-bit LSB executable, Intel 80386,'):
    deb.arch = 'i386'
  elif arch.startswith('ELF 64-bit LSB executable, x86-64,'):
    deb.arch = 'amd64'
  else:
    print('unsupported architecture', arch)
    sys.exit(1)

  with tempfile.TemporaryDirectory() as builddir:
    print('created temporary directory', builddir)
    deb.build = builddir

  # get version and binary name
  with IniFile(os.path.join(source, 'application.ini')) as ini:
    deb.version = ini['App']['Version']
    if '-beta' in deb.version:
      deb.base = 'beta'
      deb.binary = 'zotero-beta'
    else:
      deb.base = 'release'
      deb.binary = 'zotero'

  deb.bump = ''
  deb.dependencies = []
  if 'deb' in config.ini:
    if deb.version in config.ini['deb']:
      deb.bump = '-' + config.ini['deb'][deb.version]
    if 'dependencies' in config.ini['deb']:
      deb.dependencies = [dep.strip() for dep in config.ini['deb']['dependencies'].split(',')]
  for dep in os.popen('apt-cache depends firefox-esr').read().split('\n'):
    dep = dep.strip()
    if not dep.startswith('Depends:'): continue
    dep = dep.split(':')[1].strip()
    if dep == 'lsb-release': continue # why should it need this?
    if 'gcc' in dep: continue #43
    deb.dependencies.append(dep)
  deb.dependencies = ', '.join(sorted(list(set(deb.dependencies))))
  deb.description = config.ini['deb']['description']
  deb.deb = os.path.join(config.base, deb.base, deb.version, f'{deb.binary}_{deb.version}{deb.bump}_{deb.arch}.deb')

  # copy zotero to the build directory, excluding the desktpo file (which we'll recreate later) and the update files
  os.makedirs(os.path.join(deb.build, 'usr/lib'), exist_ok=True)
  shutil.copytree(source, os.path.join(deb.build, 'usr/lib', deb.binary), ignore=shutil.ignore_patterns('zotero.desktop', 'active-update.xml', 'precomplete', 'removed-files', 'updates', 'updates.xml'))
  if deb.binary != 'zotero':
    # rename the 'zotero' binary to 'zotero-beta' for the beta package so they can be installed alongside each other
    shutil.move(os.path.join(deb.build, 'usr/lib', deb.binary, 'zotero'), os.path.join(deb.build, 'usr/lib', deb.binary, deb.binary))

  class Open():
    def __init__(self, path, mode='r', fmode=None):
      self.path = os.path.join(deb.build, path)
      if 'w' in mode or 'a' in mode: os.makedirs(os.path.dirname(self.path), exist_ok=True)
      self.mode = fmode
      self.f = open(self.path, mode)

    def __enter__(self):
      return self.f

    def __exit__(self, exc_type, exc_value, exc_traceback):
      self.f.close()
      if self.mode is not None:
        os.chmod(self.path, self.mode)

  # disable auto-update
  with Open(f'usr/lib/{deb.binary}/defaults/pref/local-settings.js', 'a') as ls, Open(f'usr/lib/{deb.binary}/mozilla.cfg', 'a') as cfg:
    # enable mozilla.cfg
    if ls.tell() != 0: print('', file=ls)
    print('pref("general.config.obscure_value", 0); // only needed if you do not want to obscure the content with ROT-13', file=ls)
    print('pref("general.config.filename", "mozilla.cfg");', file=ls)

    # disable auto-update
    if cfg.tell() == 0:
      print('//', file=cfg)
    else:
      print('', file=cfg)
    print('lockPref("app.update.enabled", false);', file=cfg)
    print('lockPref("app.update.auto", false);', file=cfg)

  # create desktop file
  with IniFile(os.path.join(source, 'zotero.desktop')) as ini:
    deb.section = ini['Desktop Entry']['Categories'].rstrip(';')
    ini.set('Desktop Entry', 'Exec', f'/usr/lib/{deb.binary}/{deb.binary} --url %u')
    ini.set('Desktop Entry', 'Icon', f'/usr/lib/{deb.binary}/chrome/icons/default/default256.png')
    ini.set('Desktop Entry', 'MimeType', ';'.join([
      'x-scheme-handler/zotero',
      'application/x-endnote-refer',
      'application/x-research-info-systems',
      'text/ris',
      'text/x-research-info-systems',
      'application/x-inst-for-Scientific-info',
      'application/mods+xml',
      'application/rdf+xml',
      'application/x-bibtex',
      'text/x-bibtex',
      'application/marc',
      'application/vnd.citationstyles.style+xml'
    ]))
    ini.set('Desktop Entry', 'Description', deb.description)
    with Open(f'usr/share/applications/{deb.binary}.desktop', 'w') as f:
      ini.write(f, space_around_delimiters=False)

  # add mime info
  with open(config.mime) as mime, Open(f'usr/share/mime/packages/{deb.binary}.xml', 'w') as f:
    f.write(mime.read())

  #write build control file
  with Open('DEBIAN/control', 'w') as f:
    print(f'Package: {deb.binary}', file=f)
    print(f'Architecture: {deb.arch}', file=f)
    print(f'Depends: {deb.dependencies}', file=f)
    print(f'Maintainer: {config.maintainer}', file=f)
    print(f'Section: {deb.section}', file=f)
    print('Priority: optional', file=f)
    print(f'Version: {deb.version}{deb.bump}', file=f)
    print(f'Description: {deb.description}', file=f)

  # create symlink to binary
  os.makedirs(os.path.join(deb.build, 'usr/local/bin'))
  os.symlink(f'/usr/lib/{deb.binary}/{deb.binary}', os.path.join(deb.build, 'usr/local/bin', deb.binary))

  def run(cmd):
    print('$', cmd)
    print(subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT).decode('utf-8'))

  # build deb
  if os.path.exists(deb.deb):
    os.remove(deb.deb)
  os.makedirs(os.path.dirname(deb.deb), exist_ok=True)
  run(f'fakeroot dpkg-deb --build -Zgzip {shlex.quote(deb.build)} {shlex.quote(deb.deb)}')
  run(f'dpkg-sig -k {shlex.quote(config.gpgkey)} --sign builder {shlex.quote(deb.deb)}')

  # rebuild repo
  repo = shlex.quote(config.repo)
  gpgkey = shlex.quote(config.gpgkey)
  deb_gpg_key = shlex.quote(os.path.join(config.repo, 'deb.gpg.key'))
  packages = shlex.quote(os.path.join(config.repo, "Packages"))
  release = shlex.quote(os.path.join(config.repo, "Release"))
  release_gpg = shlex.quote(os.path.join(config.repo, "Release.gpg"))
  inrelease = shlex.quote(os.path.join(config.repo, "InRelease"))
  run(f'gpg --armor --export {gpgkey} > {deb_gpg_key}')
  run(f'cd {repo} && apt-ftparchive packages . > {packages}')
  run(f'bzip2 -kf {packages}')
  run(f'cd {repo} && apt-ftparchive release . > Release')
  run(f'gpg --yes -abs -u {gpgkey} -o {release_gpg} --digest-algo sha256 {release}')
  run(f'gpg --yes -abs -u {gpgkey} --clearsign -o {inrelease} --digest-algo sha256 {release}')
