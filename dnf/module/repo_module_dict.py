# Copyright (C) 2017-2018  Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import fnmatch
import os
from collections import OrderedDict

import hawkey
import libdnf.smartcols
import libdnf.module
import dnf.selector

from dnf.conf.read import ModuleReader
from dnf.module import module_messages, NOTHING_TO_SHOW, \
    INSTALLING_NEWER_VERSION, ENABLED_MODULES
from dnf.module.exceptions import NoStreamSpecifiedException, NoModuleException, \
    ProfileNotInstalledException, NoProfileToRemoveException, \
    DifferentStreamEnabledException, EnableMultipleStreamsException
from dnf.module.repo_module import RepoModule
from dnf.module.subject import ModuleSubject
from dnf.selector import Selector
from dnf.subject import Subject
from dnf.util import first_not_none, logger, ensure_dir
from dnf.i18n import _, P_, ucd

STATE_DEFAULT = libdnf.module.ModulePackageContainer.ModuleState_DEFAULT
STATE_ENABLED = libdnf.module.ModulePackageContainer.ModuleState_ENABLED
STATE_DISABLED = libdnf.module.ModulePackageContainer.ModuleState_DISABLED
STATE_UNKNOWN = libdnf.module.ModulePackageContainer.ModuleState_UNKNOWN

class RepoModuleDict(OrderedDict):

    def __init__(self, base):
        super(RepoModuleDict, self).__init__()

        self.base = base

    def add(self, repo_module_version):
        module = self.setdefault(repo_module_version.name, RepoModule())
        module.add(repo_module_version)
        module.parent = self

    def find_module_version(self, name, stream=None, version=None, context=None, arch=None):
        def use_enabled_stream(repo_module):
            if repo_module.conf.state._get() == "enabled":
                return repo_module.conf.stream._get()
            return None

        def use_default_stream(repo_module):
            return repo_module.defaults.peek_default_stream()

        try:
            repo_module = self[name]

            stream = first_not_none([stream,
                                     use_enabled_stream(repo_module),
                                     use_default_stream(repo_module)])

            if not stream:
                raise NoStreamSpecifiedException(name)

            repo_module_stream = repo_module[stream]

            if version:
                repo_module_version = repo_module_stream[version]
            else:
                # if version is not specified, pick the latest
                repo_module_version = repo_module_stream.latest()

            # TODO: arch
            # TODO: platform module

        except KeyError:
            return None
        return repo_module_version

    def enable(self, module_specs):
        no_match_specs, error_spec, module_dicts = self._resolve_specs_enable_update_sack(module_specs)
        for spec, (nsvcap, module_dict) in module_dicts.items():
            if nsvcap.profile:
                logger.info(_("Ignoring unnecessary profile: '{}/{}'").format(nsvcap.name, nsvcap.profile))
        if no_match_specs:
            raise dnf.module.exceptions.ModuleMarkingError(no_match_specs=no_match_specs)

    def disable(self, module_specs):
        no_match_specs = self._modules_reset_or_disable(module_specs, STATE_DISABLED)
        if no_match_specs:
            raise dnf.module.exceptions.ModuleMarkingError(no_match_specs=no_match_specs)

    def _get_modules(self, module_spec):
        subj = hawkey.Subject(module_spec)
        for nsvcap in subj.nsvcap_possibilities():
            name = nsvcap.name if nsvcap.name else ""
            stream = nsvcap.stream if nsvcap.stream else ""
            version = ""
            context = nsvcap.context if nsvcap.context else ""
            arch = nsvcap.arch if nsvcap.arch else ""
            if nsvcap.version and nsvcap.version != -1:
                version = str(nsvcap.version)
            modules = self.base._moduleContainer.query(name, stream, version, context, arch)
            if modules:
                return modules, nsvcap
        return (), None

    def _get_latest(self, module_list):
        latest = None
        if module_list:
            latest = module_list[0]
            for module in module_list[1:]:
                if module.getVersion() > latest.getVersion():
                    latest = module
        return latest

    def _create_module_dict_and_enable(self, module_list, enable=True):
        moduleDict = {}
        for module in module_list:
            moduleDict.setdefault(
                module.getName(), {}).setdefault(module.getStream(), []).append(module)

        for moduleName, streamDict in moduleDict.items():
            moduleState = self.base._moduleContainer.getModuleState(moduleName)
            if len(streamDict) > 1:
                if moduleState != STATE_DEFAULT and moduleState != STATE_ENABLED:
                    raise EnableMultipleStreamsException(moduleName)
                if moduleState == STATE_ENABLED:
                    stream = self.base._moduleContainer.getEnabledStream(moduleName)
                else:
                    stream = self.base._moduleContainer.getDefaultStream(moduleName)
                if stream not in streamDict:
                    raise EnableMultipleStreamsException(moduleName)
                for key in sorted(streamDict.keys()):
                    if key == stream:
                        if enable:
                            self.base._moduleContainer.enable(moduleName, key)
                        continue
                    del streamDict[key]
            elif enable:
                for key in streamDict.keys():
                    self.base._moduleContainer.enable(moduleName, key)
            assert len(streamDict) == 1
        return moduleDict

    def _resolve_specs_enable_update_sack(self, module_specs):
        no_match_specs = []
        error_spec = []
        module_dicts = {}
        for spec in set(module_specs):
            module_list, nsvcap = self._get_modules(spec)
            if not module_list:
                no_match_specs.append(spec)
                continue
            try:
                module_dict = self._create_module_dict_and_enable(module_list, True)
                module_dicts[spec] = (nsvcap, module_dict)
            except (RuntimeError, EnableMultipleStreamsException) as e:
                error_spec.append(spec)
                logger.error(ucd(e))
                logger.error(_("Unable to resolve argument {}").format(spec))
        hot_fix_repos = [i.id for i in self.base.repos.iter_enabled() if i.module_hotfixes]
        self.base.sack.filter_modules(self.base._moduleContainer, hot_fix_repos,
                                      self.base.conf.installroot, None)
        return no_match_specs, error_spec, module_dicts

    def install(self, module_specs, strict=True):
        no_match_specs, error_specs, module_dicts = self._resolve_specs_enable_update_sack(module_specs)

        # <package_name, set_of_spec>
        install_dict = {}
        install_set_artefacts = set()
        for spec, (nsvcap, moduledict) in module_dicts.items():
            for name, streamdict in moduledict.items():
                for stream, module_list in streamdict.items():
                    install_module_list = [x for x in module_list if self.base._moduleContainer.isModuleActive(x.getId())]
                    if not install_module_list:
                        error_specs.append(spec)
                        continue
                    profiles = []
                    latest_module = self._get_latest(install_module_list)
                    if nsvcap.profile:
                        profiles.extend(latest_module.getProfiles(nsvcap.profile))
                        if not profiles:
                            logger.error(_("Unable to match profile in argument {}").format(spec))
                            error_specs.append(spec)
                            continue
                    else:
                        profiles_strings = self.base._moduleContainer.getDefaultProfiles(name, stream)
                        if not profiles_strings:
                            logger.error(_("No default profiles for module {}:{}").format(name, stream))
                            profiles_strings = ['default']
                        for profile in set(profiles_strings):
                            module_profiles = latest_module.getProfiles(profile)
                            if not module_profiles:
                                logger.error(_("Default profile {} not matched for module {}:{}").format(profile, name,
                                                                                                         stream))
                                if profiles_strings == ['default']:
                                    self.base._moduleContainer.install(latest_module, 'default')

                            profiles.extend(module_profiles)
                    for profile in profiles:
                        self.base._moduleContainer.install(latest_module ,profile.getName())
                        for pkg_name in profile.getContent():
                            install_dict.setdefault(pkg_name, set()).add(spec)
                    for module in install_module_list:
                        install_set_artefacts.update(module.getArtifacts())
        install_base_query = self.base.sack.query().filterm(nevra_strict=install_set_artefacts).apply()

        for pkg_name, set_specs in install_dict.items():
            query = install_base_query.filter(name=pkg_name)
            if not query:
                for spec in set_specs:
                    logger.error(_("Unable to resolve argument {}").format(spec))
                logger.error(_("No match for package {}").format(pkg_name))
                error_specs.extend(set_specs)
                continue
            self.base._goal.group_members.add(pkg_name)
            sltr = dnf.selector.Selector(self.base.sack)
            sltr.set(pkg=query)
            self.base._goal.install(select=sltr, optional=(not strict))
        if no_match_specs or error_specs:
            raise dnf.module.exceptions.ModuleMarkingError(no_match_specs=no_match_specs,
                                                           error_specs=error_specs)

    def _modules_reset_or_disable(self, module_specs, to_state):
        no_match_specs = []
        for spec in set(module_specs):
            module_list, nsvcap = self._get_modules(spec)
            if not module_list:
                logger.error(_("Unable to resolve argument {}").format(spec))
                no_match_specs.append(spec)
                continue
            if nsvcap.profile:
                logger.info(_("Ignoring unnecessary profile: '{}/{}'").format(nsvcap.name, nsvcap.profile))
            module_names = set()
            for module in module_list:
                module_names.add(module.getName())
            for name in module_names:
                if to_state == STATE_UNKNOWN:
                    self.base._moduleContainer.reset(name)
                if to_state == STATE_DISABLED:
                    self.base._moduleContainer.disable(name)

        hot_fix_repos = [i.id for i in self.base.repos.iter_enabled() if i.module_hotfixes]
        self.base.sack.filter_modules(self.base._moduleContainer, hot_fix_repos,
                                      self.base.conf.installroot, self.base.conf.module_platform_id,
                                      update_only=True)
        return no_match_specs

    def reset(self, module_specs):
        self._modules_reset_or_disable(module_specs, STATE_UNKNOWN)

    def _get_package_name_set_and_remove_profiles(self, module_list, nsvcap, remove=False):
        package_name_set = set()
        latest_module = self._get_latest(module_list)
        installed_profiles_strings = set(self.base._moduleContainer.getInstalledProfiles(latest_module.getName()))
        if not installed_profiles_strings:
            return set()
        if nsvcap.profile:
            profiles_set = latest_module.getProfiles(nsvcap.profile)
            if not profiles_set:
                return set()
            for profile in profiles_set:
                if profile.getName() in installed_profiles_strings:
                    if remove:
                        self.base._moduleContainer.uninstall(latest_module, profile.getName())
                    package_name_set.update(profile.getContent())
        else:
            for profile_string in installed_profiles_strings:
                if remove:
                    self.base._moduleContainer.uninstall(latest_module, profile_string)
                for profile in latest_module.getProfiles(profile_string):
                    package_name_set.update(profile.getContent())
        return package_name_set

    def upgrade(self, module_specs):
        no_match_specs = []

        for spec in set(module_specs):
            module_list, nsvcap = self._get_modules(spec)
            if not module_list:
                no_match_specs.append(spec)
                continue
            update_module_list = [x for x in module_list if self.base._moduleContainer.isModuleActive(x.getId())]
            if not update_module_list:
                logger.error(_("Unable to resolve argument {}").format(spec))
                continue
            module_dict = self._create_module_dict_and_enable(update_module_list, False)
            upgrade_package_set = set()
            for name, streamdict in module_dict.items():
                for stream, module_list_from_dict in streamdict.items():
                    upgrade_package_set.update(self._get_package_name_set_and_remove_profiles(module_list_from_dict, nsvcap))
                    latest_module = self._get_latest(module_list_from_dict)
                    installed_profiles_strings = set(self.base._moduleContainer.getInstalledProfiles(latest_module.getName()))
                    if not installed_profiles_strings:
                        continue
                    if nsvcap.profile:
                        profiles_set = latest_module.getProfiles(nsvcap.profile)
                        if not profiles_set:
                            continue
                        for profile in profiles_set:
                            if profile.getName() in installed_profiles_strings:
                                upgrade_package_set.update(profile.getContent())
                    else:
                        for profile_string in installed_profiles_strings:
                            for profile in latest_module.getProfiles(profile_string):
                                upgrade_package_set.update(profile.getContent())
            if not upgrade_package_set:
                logger.error(_("Unable to match profile in argument {}").format(spec))
            query = self.base.sack.query().available().filterm(name=upgrade_package_set)
            if query:
                sltr = Selector(self.base.sack)
                sltr.set(pkg=query)
                self.base._goal.upgrade(select=sltr)
        return no_match_specs

    def remove(self, module_specs):
        no_match_specs = []
        remove_package_set = set()

        for spec in set(module_specs):
            module_list, nsvcap = self._get_modules(spec)
            if not module_list:
                no_match_specs.append(spec)
                continue
            module_dict = self._create_module_dict_and_enable(module_list, False)
            remove_packages_names = []
            for name, streamdict in module_dict.items():
                for stream, module_list_from_dict in streamdict.items():
                    remove_packages_names.extend(self._get_package_name_set_and_remove_profiles(module_list_from_dict, nsvcap, True))
            if not remove_packages_names:
                logger.error(_("Unable to match profile in argument {}").format(spec))
            remove_package_set.update(remove_packages_names)

        if remove_package_set:
            keep_pkg_names = self.base._moduleContainer.getInstalledPkgNames()
            remove_package_set.difference(keep_pkg_names)
            if remove_package_set:
                query = self.base.sack.query().installed().filterm(name=remove_package_set)
                if query:
                    self.base._remove_if_unneeded(query)
        return no_match_specs

    def read_all_module_confs(self):
        module_reader = ModuleReader(self.get_modules_dir())
        for conf in module_reader:
            repo_module = self.setdefault(conf.name._get(), RepoModule())
            repo_module.conf = conf
            repo_module.name = conf.name._get()
            repo_module.parent = self

    def get_modules_dir(self):
        modules_dir = os.path.join(self.base.conf.installroot,
                                   self.base.conf.modulesdir._get().lstrip("/"))

        ensure_dir(modules_dir)

        return modules_dir

    def _get_info_profiles(self, module_specs):
        output = []
        for module_spec in module_specs:
            module_list, nsvcap = self._get_modules(module_spec)
            if not module_list:
                logger.info(_("Unable to resolve argument {}").format(module_spec))
                continue

            if nsvcap.profile:
                logger.info(_("Ignoring unnecessary profile: '{}/{}'").format(
                    nsvcap.name, nsvcap.profile))
            for module in module_list:

                lines = OrderedDict()
                lines["Name"] = module.getFullIdentifier()

                for profile in module.getProfiles():
                    lines[profile.getName()] = "\n".join([pkgName for pkgName in profile.getContent()])

                output.append(self._create_simple_table(lines).toString())
        return "\n\n".join(sorted(output))

    def _get_info(self, module_specs):
        output = []
        for module_spec in module_specs:
            module_list, nsvcap = self._get_modules(module_spec)
            if not module_list:
                logger.info(_("Unable to resolve argument {}").format(module_spec))
                continue

            if nsvcap.profile:
                logger.info(_("Ignoring unnecessary profile: '{}/{}'").format(
                    nsvcap.name, nsvcap.profile))
            for modulePackage in module_list:
                default_str = ""
                if modulePackage.getStream == self.base._moduleContainer.getDefaultStream(
                        modulePackage.getName()):
                    default_str = " [d]"
                enabled_str = ""
                if self.base._moduleContainer.isEnabled(modulePackage):
                    if not default_str:
                        enabled_str = " "
                    enabled_str += "[e]"
                installed_profiles = self.base._moduleContainer.getInstalledProfiles(
                    modulePackage.getName())
                available_profiles = modulePackage.getProfiles()

                default_profiles = self.base._moduleContainer.getDefaultProfiles(
                    modulePackage.getName(), modulePackage.getStream())
                profiles_str = ""
                for profile in available_profiles:
                    profiles_str += "{}{}".format(profile.getName(),
                                                  " [d]" if profile.getName() in default_profiles else "")
                    profiles_str += "[i], " if profile in installed_profiles else ", "

                profiles_str = profiles_str[:-2]

                lines = OrderedDict()
                lines["Name"] = modulePackage.getName()
                lines["Stream"] = modulePackage.getStream() + default_str + enabled_str
                lines["Version"] = modulePackage.getVersion()
                lines["Profiles"] = profiles_str
                lines["Default profiles"] = " ".join(default_profiles)
                lines["Repo"] = modulePackage.getRepoID()
                lines["Summary"] = modulePackage.getSummary()
                lines["Description"] = modulePackage.getDescription()
                lines["Artifacts"] = "\n".join(sorted(modulePackage.getArtifacts()))
                output.append(self._create_simple_table(lines).toString())
        str_table = "\n\n".join(sorted(set(output)))
        return str_table + "\n\nHint: [d]efault, [e]nabled, [i]nstalled"

    @staticmethod
    def _create_simple_table(lines):
        table = libdnf.smartcols.Table()
        table.enableNoheadings(True)
        table.setColumnSeparator(" : ")

        column_name = table.newColumn("Name")
        column_value = table.newColumn("Value")
        column_value.setWrap(True)
        column_value.setSafechars("\n")
        column_value.setNewlineWrapFunction()

        for line_name, value in lines.items():
            if value:
                line = table.newLine()
                line.getColumnCell(column_name).setData(line_name)
                line.getColumnCell(column_value).setData(str(value))

        return table

    def _get_full_info(self, module_specs):
        output = []
        for module_spec in module_specs:
            module_list, nsvcap = self._get_modules(module_spec)
            if not module_list:
                logger.info(_("Unable to resolve argument {}").format(module_spec))
                continue

            if nsvcap.profile:
                logger.info(_("Ignoring unnecessary profile: '{}/{}'").format(
                    nsvcap.name, nsvcap.profile))
            for modulePackage in module_list:
                info = modulePackage.getYaml()
                if info:
                    output.append(info)
        output_string = "\n\n".join(sorted(set(output)))
        return output_string

    def list_module_version_latest(self):
        versions = []

        for module in self.values():
            for stream in module.values():
                versions.append(stream.latest())

        return versions

    def list_module_version_installed(self):
        versions = []

        for version in self.list_module_version_latest():
            conf = version.repo_module.conf
            if conf.state._get() == "enabled" and conf.stream._get() == version.stream \
                    and list(conf.profiles._get()):
                versions.append(version)

        return versions

    def _what_provides(self, rpm_specs):
        output = []
        modulePackages = self.base._moduleContainer.getModulePackages()
        baseQuery = self.base.sack.query().filterm(empty=True).apply()
        for spec in rpm_specs:
            subj = dnf.subject.Subject(spec)
            baseQuery = baseQuery.union(subj.get_best_query(
                self.base.sack, with_nevra=True, with_provides=False, with_filenames=False))
        baseQuery.apply()

        for modulePackage in modulePackages:
            artifacts = modulePackage.getArtifacts()
            if not artifacts:
                continue
            query = baseQuery.filter(nevra_strict=artifacts)
            if query:
                for pkg in query:
                    string_output = ""
                    profiles = []
                    for profile in modulePackage.getProfiles():
                        if pkg.name in profile.getContent():
                            profiles.append(profile.getName())

                    lines = {"Module": modulePackage.getFullIdentifier(),
                             "Profiles": " ".join(profiles),
                             "Repo": modulePackage.getRepoID(),
                             "Summary": modulePackage.getSummary()}

                    table = self._create_simple_table(lines)

                    string_output += "{}\n".format(self.base.output.term.bold(str(pkg)))
                    string_output += "{}".format(table.toString())
                    output.append(string_output)

        return "\n\n".join(sorted(output))

    def _create_and_fill_table(self, latest):
        table = libdnf.smartcols.Table()
        table.setTermforce(libdnf.smartcols.Table.TermForce_ALWAYS)
        table.enableMaxout(True)
        column_name = table.newColumn("Name")
        column_stream = table.newColumn("Stream")
        column_profiles = table.newColumn("Profiles")
        column_profiles.setWrap(True)
        column_info = table.newColumn("Summary")
        column_info.setWrap(True)

        if not self.base.conf.verbose:
            column_info.hidden = True

        for latest_per_repo in latest:
            for nameStreamArch in latest_per_repo:
                if len(nameStreamArch) == 1:
                    modulePackage = nameStreamArch[0]
                else:
                    active = [module for module in nameStreamArch if self.base._moduleContainer.isModuleActive(module)]
                    if active:
                        modulePackage = active[0]
                    else:
                        modulePackage = nameStreamArch[0]
                line = table.newLine()
                default_str = ""
                enabled_str = ""
                profiles_str = ""
                available_profiles = modulePackage.getProfiles()

                if modulePackage.getStream == self.base._moduleContainer.getDefaultStream(modulePackage.getName()):
                    default_str = " [d]"

                if self.base._moduleContainer.isEnabled(modulePackage):
                    if not default_str:
                        enabled_str = " "
                    enabled_str += "[e]"

                installed_profiles = self.base._moduleContainer.getInstalledProfiles(modulePackage.getName())

                default_profiles = self.base._moduleContainer.getDefaultProfiles(modulePackage.getName(), modulePackage.getStream())
                for profile in available_profiles:
                    profile_name = profile.getName()
                    profiles_str += "{}{}".format(profile_name,
                                                  " [d]" if profile_name in default_profiles else "")
                    profiles_str += "[i], " if profile_name in installed_profiles else ", "

                line.getColumnCell(column_name).setData(modulePackage.getName())
                line.getColumnCell(column_stream).setData(modulePackage.getStream() + default_str + enabled_str)
                line.getColumnCell(column_profiles).setData(profiles_str[:-2])
                line.getColumnCell(column_info).setData(modulePackage.getSummary())

        return table

    def _get_brief_description(self, module_specs, module_state):
        modules = []
        if module_specs:
            for spec in set(module_specs):
                module_list, nsvcap = self._get_modules(spec)
                modules.extend(module_list)
        else:
            modules = self.base._moduleContainer.getModulePackages()
        latest = self.base._moduleContainer.getLatestModulesPerRepo(module_state, modules)
        if not latest:
            return module_messages[NOTHING_TO_SHOW]

        table = self._create_and_fill_table(latest)
        current_repo_id_index = 0
        already_printed_lines = 0
        repo_name = self.base.repos[latest[0][0][0].getRepoID()].name
        versions = len(latest[0])
        str_table = self._print_header(table, repo_name, True)
        for i in range(0, table.getNumberOfLines()):
            if versions + already_printed_lines <= i:
                already_printed_lines += versions
                current_repo_id_index += 1
                repo_name = self.base.repos[latest[current_repo_id_index][0][0].getRepoID()].name
                versions = len(latest[current_repo_id_index])
                str_table += "\n"
                str_table += self._print_header(table, repo_name, False)

            line = table.getLine(i)
            str_table += table.toString(line, line)

        return str_table + "\n\nHint: [d]efault, [e]nabled, [i]nstalled"

    def _print_header(self, table, repo_id, with_header):
        # TODO Fix header to print it always - now not accessible in second call.
        line = table.getLine(0)
        header = table.toString(line, line).split('\n', 1)[0]
        out_str = "{}\n".format(self.base.output.term.bold(repo_id))
        if with_header:
            out_str += "{}\n".format(header)
        return out_str
