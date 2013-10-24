#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Licensed under the GNU General Public License Version 2
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.

# Copyright (C) 2009-2013
#    Richard Hughes <richard@hughsie.com>
#
# Copyright (C) 2013
#    Florian Festi <ffesti@redhat.com>
#

import glob
import os
import shutil
import subprocess
import sys
import tarfile
import fnmatch
from gi.repository import Gio

# internal
from logger import LoggerItem
from package import Package
from appdata import AppData
from config import Config
from desktop_file import DesktopFile
from font_file import FontFile
from input_method import InputMethodTable, InputMethodComponent
from codec import Codec

def package_decompress(pkg):
    if os.path.exists('../extract-package'):
        p = subprocess.Popen(['../extract-package', pkg.filename, 'tmp'],
                             cwd='.', stdout=subprocess.PIPE)
        p.wait()
        if p.returncode:
            raise StandardError('Cannot extract package: ' + p.stdout)
    else:
        wildcards = []
        if not os.getenv('APPSTREAM_DEBUG'):
            wildcards.append('./usr/share/applications/*.desktop')
            wildcards.append('./usr/share/applications/kde4/*.desktop')
            wildcards.append('./usr/share/appdata/*.xml')
            wildcards.append('./usr/share/icons/hicolor/*/apps/*')
            wildcards.append('./usr/share/pixmaps/*.*')
            wildcards.append('./usr/share/icons/*.*')
            wildcards.append('./usr/share/*/images/*')
            pkg.extract('./tmp', wildcards)
        else:
            wildcards.append('./*/*.*')
            pkg.extract('./tmp', wildcards)

class Build:

    def __init__(self):
        self.cfg = Config()
        self.application_ids = []
        self.has_valid_content = False
        self.status_html = None

    def add_use_appdata_file(self, app, filename):
        data = AppData()
        if not data.extract(filename):
            app.log.write(LoggerItem.WARNING,
                          "AppData file '%s' could not be parsed" % filename)
            return

        # check AppData file validates
        if os.path.exists('/usr/bin/appdata-validate'):
            env = os.environ
            p = subprocess.Popen(['/usr/bin/appdata-validate',
                                  '--relax', filename],
                                 cwd='.', env=env, stdout=subprocess.PIPE)
            p.wait()
            if p.returncode:
                for line in p.stdout:
                    line = line.replace('\n', '')
                    app.log.write(LoggerItem.WARNING,
                                  "AppData did not validate: %s" % line)

        # check the id matches
        if data.get_id() != app.app_id and data.get_id() != app.app_id_full:
            app.log.write(LoggerItem.WARNING,
                          "The AppData id does not match: " + app.app_id)
            return

        # check the licence is okay
        if data.get_licence() not in self.cfg.get_content_licences():
            app.log.write(LoggerItem.WARNING,
                          "The AppData licence is not okay for " +
                          app.app_id + ': \'' +
                          data.get_licence() + '\'')
            return

        # if we have an override, use it for all languages
        tmp = data.get_names()
        if tmp:
            app.names = tmp

        # if we have an override, use it for all languages
        tmp = data.get_summaries()
        if tmp:
            app.comments = tmp

        # get optional bits
        tmp = data.get_urls()
        if tmp:
            for key in tmp:
                app.urls[key] = tmp[key]
        tmp = data.get_project_group()
        if tmp:
            app.project_group = tmp
        app.descriptions = data.get_descriptions()

        # get screenshots
        tmp = data.get_screenshots()
        for image in tmp:
            app.log.write(LoggerItem.INFO, "downloading %s" % image)
            app.add_screenshot_url(image)

        # get compulsory_for_desktop
        for c in data.get_compulsory_for_desktop():
            if c not in app.compulsory_for_desktop:
                app.compulsory_for_desktop.append(c)

    def add_application(self, app):

        # application is blacklisted
        blacklisted = False
        for b in self.cfg.get_id_blacklist():
            if fnmatch.fnmatch(app.app_id, b):
                app.log.write(LoggerItem.INFO, "application is blacklisted")
                blacklisted = True
                break
        if blacklisted:
            return False

        # packages that ship .desktop files in /usr/share/applications
        # *and* /usr/share/applications/kde4 do not need multiple entries
        if app.app_id in self.application_ids:
            app.log.write(LoggerItem.INFO, "duplicate ID in package %s" % app.pkgname)
            return False
        self.application_ids.append(app.app_id)

        # do we have an AppData file?
        appdata_file = './tmp/usr/share/appdata/' + app.app_id + '.appdata.xml'
        appdata_extra_file = '../appdata-extra/' + app.type_id + '/' + app.app_id + '.appdata.xml'
        if os.path.exists(appdata_file) and os.path.exists(appdata_extra_file):
            app.log.write(LoggerItem.INFO, "deleting %s as upstream AppData file exists" % appdata_extra_file)
            os.remove(appdata_extra_file)

        # just use the extra file in places of the missing upstream one
        if os.path.exists(appdata_extra_file):
            appdata_file = appdata_extra_file

        # need to extract details
        if os.path.exists(appdata_file):
            self.add_use_appdata_file(app, appdata_file)
        elif app.requires_appdata:
            app.log.write(LoggerItem.INFO, "%s requires AppData" % app.app_id_full)
            return False

        # use the homepage to filter out same more generic apps
        homepage_url = None
        if app.urls.has_key('homepage'):
            homepage_url = app.urls['homepage']
        if homepage_url and not app.project_group:

            # GNOME
            project_urls = [ 'http*://*.gnome.org*',
                             'http://gnome-*.sourceforge.net/']
            for m in project_urls:
                if fnmatch.fnmatch(homepage_url, m):
                    app.project_group = "GNOME"

            # KDE
            project_urls = [ 'http*://*.kde.org*',
                            'http://*kde-apps.org/*' ]
            for m in project_urls:
                if fnmatch.fnmatch(homepage_url, m):
                    app.project_group = "KDE"

            # XFCE
            project_urls = [ 'http://*xfce.org*' ]
            for m in project_urls:
                if fnmatch.fnmatch(homepage_url, m):
                    app.project_group = "XFCE"

            # LXDE
            project_urls = [ 'http://lxde.org*',
                             'http://lxde.sourceforge.net/*' ]
            for m in project_urls:
                if fnmatch.fnmatch(homepage_url, m):
                    app.project_group = "LXDE"

            # MATE
            project_urls = [ 'http://*mate-desktop.org*' ]
            for m in project_urls:
                if fnmatch.fnmatch(homepage_url, m):
                    app.project_group = "MATE"

            # print that we auto-added it
            if app.project_group:
                app.log.write(LoggerItem.INFO, "assigned %s" % app.project_group)

        # Do not include apps without a name
        if not 'C' in app.names:
            app.log.write(LoggerItem.INFO, "ignored as no Name")
            return False

        # Do not include apps without a summary
        if not 'C' in app.comments:
            app.log.write(LoggerItem.INFO, "ignored as no Comment")
            return False

        # Do not include apps without an icon
        if not app.icon:
            app.log.write(LoggerItem.INFO, "ignored as no Icon")
            return False

        # do we have screeshot overrides?
        extra_screenshots = os.path.join('../screenshots-extra', app.app_id)
        if os.path.exists(extra_screenshots):
            app.screenshots = []
            overrides = glob.glob(extra_screenshots + "/*.png")
            app.log.write(LoggerItem.INFO, "adding %i screenshot overrides" % len(overrides))
            overrides.sort()
            for f in overrides:
                app.add_screenshot_filename(f)

        # we got something useful
        if not self.has_valid_content:
            self.has_valid_content = True

        # write the status HTML page if enabled
        if not self.status_html:
            self.status_html = open('logs/status.html', 'w')
        app.status_html = self.status_html

        return True

    def build(self, filename):

        # check the package has .desktop files
        pkg = Package(filename)

        for b in self.cfg.get_package_blacklist():
            if fnmatch.fnmatch(pkg.name, b):
                pkg.log.write(LoggerItem.INFO, "package is blacklisted")
                return

        # set up state
        if not os.path.exists('./appstream'):
            os.makedirs('./appstream')
        if not os.path.exists('./icons'):
            os.makedirs('./icons')
        if not os.path.exists('./screenshot-cache'):
            os.makedirs('./screenshot-cache')
        if not os.path.exists('./screenshots'):
            os.makedirs('./screenshots')
        if not os.path.exists('./screenshots/source'):
            os.makedirs('./screenshots/source')
        for size in self.cfg.get_screenshot_thumbnail_sizes():
            path = './screenshots/' + str(size[0]) + 'x' + str(size[1])
            if not os.path.exists(path):
                os.makedirs(path)

        # remove tmp
        if os.path.exists('./tmp'):
            shutil.rmtree('./tmp')
        os.makedirs('./tmp')

        # decompress main file and search for desktop files
        package_decompress(pkg)
        files = []
        for f in self.cfg.get_interesting_installed_files():
            files.extend(glob.glob("./tmp" + f))
        files.sort()

        # we only need to install additional files if we're not running on
        # the builders
        for c in self.cfg.get_package_data_list():
            if fnmatch.fnmatch(pkg.name, c[0]):
                extra_files = glob.glob("./packages/%s*.rpm" % c[1])
                for f in extra_files:
                    extra_pkg = Package(f)
                    pkg.log.write(LoggerItem.INFO, "adding extra package %s" % extra_pkg.name)
                    package_decompress(extra_pkg)

        # check for duplicate apps in the package
        self.has_valid_content = False
        valid_apps = []

        # check for codecs
        if pkg.name.startswith('gstreamer'):
            app = Codec(pkg, self.cfg)
            if app.parse_files(files):
                if self.add_application(app):
                    valid_apps.append(app)
        else:
            # process each desktop file in the original package
            for f in files:

                pkg.log.write(LoggerItem.INFO, "reading %s" % f)
                fi = Gio.file_new_for_path(f)
                info = fi.query_info('standard::content-type', 0, None)

                # create the right object depending on the content type
                content_type = info.get_content_type()
                if content_type == 'inode/symlink':
                    continue
                if content_type == 'application/x-font-ttf':
                    app = FontFile(pkg, self.cfg)
                elif content_type == 'application/x-font-otf':
                    app = FontFile(pkg, self.cfg)
                elif content_type == 'application/x-desktop':
                    app = DesktopFile(pkg, self.cfg)
                elif content_type == 'application/xml':
                    app = InputMethodComponent(pkg, self.cfg)
                elif content_type == 'application/x-sqlite3':
                    app = InputMethodTable(pkg, self.cfg)
                else:
                    pkg.log.write(LoggerItem.INFO, "content type %s not supported" % content_type)
                    continue

                # the ID is the filename unless specified otherwise
                if app.app_id is None:
                    app.set_id(f.split('/')[-1])

                # parse file
                if not app.parse_file(f):
                    continue

                # write the application
                if self.add_application(app):
                    valid_apps.append(app)

        # create AppStream XML
        if self.has_valid_content:
            xml_output_file = './appstream/' + pkg.name + '.xml'
            xml = open(xml_output_file, 'w')
            xml.write("<?xml version=\"1.0\"?>\n")
            xml.write("<applications version=\"0.1\">\n")
            for app in valid_apps:
                app.write(xml)
            xml.write("</applications>\n")
            xml.close()

            # create AppStream icon tar
            output_file = "./appstream/%s-icons.tar" % pkg.name
            pkg.log.write(LoggerItem.INFO, "writing %s and %s" % (xml_output_file, output_file))
            tar = tarfile.open(output_file, "w")
            files = glob.glob("./icons/*.png")
            for f in files:
                tar.add(f, arcname=f.split('/')[-1])
            tar.close()

        # remove tmp
        if not os.getenv('APPSTREAM_DEBUG'):
            shutil.rmtree('./tmp')
            shutil.rmtree('./icons')

def main():
    job = Build()
    for fn in sys.argv[1:]:
        job.build(fn)
    sys.exit(0)

if __name__ == "__main__":
    main()
