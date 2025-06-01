#!/usr/bin/python3

# --------------------------------------------------------------
#  ____            _   _       _     _______                                     
# |  _ \          (_) | |     | |   |__   __|                                    
# | |_) |  _   _   _  | |   __| |      | |     _ __    __ _    ___    ___   _ __ 
# |  _ <  | | | | | | | |  / _` |      | |    | '__|  / _` |  / __|  / _ \ | '__|
# | |_) | | |_| | | | | | | (_| |      | |    | |    | (_| | | (__  |  __/ | |   
# |____/   \__,_| |_| |_|  \__,_|      |_|    |_|     \__,_|  \___|  \___| |_|   
#
# --------------------------------------------------------------
#
# Copyright (c) 2025, LLC NIC CT
# Copyright (c) 2025, Vladislav Shchapov <vladislav@shchapov.ru>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses/>.
#
# --------------------------------------------------------------

import json
import multiprocessing
import os
import os.path
import re
import shutil
import subprocess
import sys

from pathlib import Path
from typing  import Final



# --------------------------------------------------------------
# Параметры PVS
#

class PVS:
    # Отображение языков программирования
    # PVS:
    #   C, C++
    map_lang_from_compiler : Final[dict] = {
        'c'  : 'C',
        'c++': 'C++'
    }

    # Отображение стандартов
    #
    # PVS:
    #   C  : "c90", "c99", "c11", "c17", "c23";
    #   C++: "c++98", "c++03", "c++11", "c++14", "c++17", "c++20", "c++23", "c++26".
    # По умолчанию для языка `C` устанавливается значение `c99`, для `C++` - `c++17`
    #
    map_std_from_compiler : Final[dict] = {
        #
        # C
        #

        'c90'           : 'c90', # gcc, clang, lcc
        'c89'           : 'c90', # gcc, clang, lcc
        'iso9899:1990'  : 'c90', # gcc, clang, lcc
        'iso9899:199409': 'c90', # gcc, clang, lcc
        'gnu90'         : 'c90', # gcc, clang, lcc
        'gnu89'         : 'c90', # gcc, clang, lcc

        'c99'           : 'c99', # gcc, clang, lcc
        'c9x'           : 'c99', # gcc       , lcc
        'iso9899:1999'  : 'c99', # gcc, clang, lcc
        'iso9899:199x'  : 'c99', # gcc
        'gnu99'         : 'c99', # gcc, clang, lcc
        'gnu9x'         : 'c99', # gcc       , lcc

        'c11'           : 'c11', # gcc, clang, lcc
        'c1x'           : 'c11', # gcc       , lcc
        'iso9899:2011'  : 'c11', # gcc, clang, lcc
        'gnu11'         : 'c11', # gcc, clang, lcc
        'gnu1x'         : 'c11', # gcc       , lcc

        'c17'           : 'c17', # gcc, clang, lcc
        'c18'           : 'c17', # gcc       , lcc
        'iso9899:2017'  : 'c17', # gcc, clang, lcc
        'iso9899:2018'  : 'c17', # gcc       , lcc
        'gnu17'         : 'c17', # gcc, clang, lcc
        'gnu18'         : 'c17', # gcc       , lcc

        'c23'           : 'c23', # gcc       , lcc
        'c2x'           : 'c23', # gcc       , lcc
        'iso9899:2024'  : 'c23', # gcc       , lcc
        'gnu23'         : 'c23', # gcc       , lcc
        'gnu2x'         : 'c23', # gcc       , lcc

        # 'c2y'   - The next version of the ISO C standard, still under development. The support for this version is experimental and incomplete.
        # 'gnu2y' - The next version of the ISO C standard, still under development, plus GNU extensions. The support for this version is experimental and incomplete.

        #
        # C++
        #

        'c++98'         : 'c++98', # gcc, clang, lcc
        'gnu++98'       : 'c++98', # gcc, clang, lcc

        'c++03'         : 'c++03', # gcc, clang, lcc
        'gnu++03'       : 'c++03', # gcc, clang, lcc

        'c++11'         : 'c++11', # gcc, clang, lcc
        'c++0x'         : 'c++11', # gcc       , lcc
        'gnu++11'       : 'c++11', # gcc, clang, lcc
        'gnu++0x'       : 'c++11', # gcc       , lcc

        'c++14'         : 'c++14', # gcc, clang, lcc
        'c++1y'         : 'c++14', # gcc       , lcc
        'gnu++14'       : 'c++14', # gcc, clang, lcc
        'gnu++1y'       : 'c++14', # gcc       , lcc

        'c++17'         : 'c++17', # gcc, clang, lcc
        'c++1z'         : 'c++17', # gcc       , lcc
        'gnu++17'       : 'c++17', # gcc, clang, lcc
        'gnu++1z'       : 'c++17', # gcc       , lcc

        'c++20'         : 'c++20', # gcc, clang, lcc
        'c++2a'         : 'c++20', # gcc       , lcc
        'gnu++20'       : 'c++20', # gcc, clang, lcc
        'gnu++2a'       : 'c++20', # gcc       , lcc

        'c++23'         : 'c++23', # gcc, clang, lcc
        'c++2b'         : 'c++23', # gcc       , lcc
        'gnu++23'       : 'c++23', # gcc, clang, lcc
        'gnu++2b'       : 'c++23', # gcc       , lcc

        'c++2c'         : 'c++26', # gcc, clang
        'c++26'         : 'c++26', # gcc
        'gnu++2c'       : 'c++26', # gcc, clang
        'gnu++26'       : 'c++26', # gcc
    }

    # Маппинг стандартов
    @staticmethod
    def map_std(std):
        # Таблица маппинга
        return PVS.map_std_from_compiler.get(std)

    # Маппинг имени языка программирования
    @staticmethod
    def map_lang(lang):
        return PVS.map_lang_from_compiler[lang]


    @staticmethod
    def default_preprocessor():
        return 'gcc'

    @staticmethod
    def map_preprocessor(comp_id, comp_like):
        for id in (comp_id, comp_like):
            if id in ['gcc', 'clang']:
                return id
            elif id == 'lcc':
                # TODO: Надо тестировать
                return 'gcc'
        return None


# --------------------------------------------------------------
# Работа
#

class BuildTraceAnalyzerPVS:
    def __split_args(self, args):
        try:
            idx = args.index('--')
            return (args[0:idx], args[idx+1:])
        except ValueError:
            return ([], args)

    def __get_parallel(self, args):
        for arg in args:
            if (mo := re.search(r"^\-\-parallel=(?P<parallel>\d+)$", arg)):
                parallel = int(mo.group('parallel'))
                return max(parallel, 1)
        return 1


    def __init__(self):
        if len(sys.argv) < 3:
            self.__print("Usage: build-tracer-analyzer-pvs.py /path/to/source/dir /path/to/result/dir [--parallel=N --] [pvs-studio args...]")
            sys.exit(1)

        self.__source_dir : Final[Path] = Path(sys.argv[1])
        self.__result_dir : Final[Path] = Path(sys.argv[2])
        ( self.__args, self.__pvs_studio_external_args ) = self.__split_args(sys.argv[3:])
        self.__parallel = self.__get_parallel(self.__args)


    def main(self):
        self.__prepare_env()
        input_json = self.__read_result_json()

        if self.__parallel > 1:
            with multiprocessing.Pool(processes=self.__parallel) as pool:
                pool.map(self.processing_item, input_json)
        else:
            for cc in input_json:
                self.processing_item(cc)


    # --------------
    # Печать строки вывода
    #
    def __print(self, *objects, sep=' ', end='\n', flush=True):
        s = ''
        if self.__parallel > 1:
            s += '{:7}:'.format(os.getpid())
            if len(objects) > 0:
                s += sep
        s += sep.join(str(item) for item in objects)
        s += end
        sys.stdout.write(s)
        if flush:
            sys.stdout.flush()


    # --------------
    # Подготовка окружения
    #
    def __prepare_env(self):
        # --------------
        # Создаем каталог выходных данных
        #
        self.__result_dir.mkdir(mode=0o755, parents=True, exist_ok=True)

        # --------------
        # Копируем chexec в root-каталог для chroot
        #
        if os.path.exists('/usr/bin/chexec'):
            shutil.copy2('/usr/bin/chexec', self.__source_dir / 'root/pvs/bin/')

        # Создаем каталог /tmp в chroot для pvs-studio:
        (self.__source_dir / 'root/tmp').mkdir(mode=0o777, parents=True, exist_ok=True)


    # --------------
    # Маскировка лицензионных данных в выводе
    #
    def __mask_lic_info(self, command):
        # --lic-name=****
        # --lic-key=****-****-****-****
        ret = []
        for arg in command:
            if isinstance(arg, str):
                if arg.startswith('--lic-name='):
                    arg = '--lic-name=****'
                elif arg.startswith('--lic-key='):
                    arg = '--lic-key=****-****-****-****'
            ret.append(arg)
        return ret

    # --------------
    # Чтение исходных данных
    #
    def __read_result_json(self):
        input_json = []
        with (self.__source_dir / 'result.json').open() as file:
            input_json = json.load(file)        
        return input_json

    # --------------
    # Обработка элемента
    #
    def processing_item(self, cc):
        # --------------

        self.__print("ANALYSIS-START------------------")
        self.__print("file:", cc['preprocessed_file'])

        # --------------

        pvs_studio_args = [
            '--platform=linux64',
            '--new-output-format=yes',
            '--disable-ms-extensions=yes',
        ]

        # ----
        # ----
        # TODO:
        #   Временная заглушка для отключения проверки путей /opt/mcst/lcc-home/ - это должен быть системный путь
        #
        #   Похоже, что средствами параметров pvs-studio не применяя --exclude-path не реализовать поведение,
        #   как для системных определений на x86

        pvs_studio_args.append('--exclude-path=/opt/mcst/lcc-home/')

        # ----
        # ----

        # Препроцессор
        if (pp := PVS.map_preprocessor(cc['command']['compiler'].get('id'), cc['command']['compiler'].get('like'))):
            pvs_studio_args.append('--preprocessor=' + pp)
        else:
            pvs_studio_args.append('--preprocessor=' + PVS.default_preprocessor())
            self.__print("WARNING: unknown preprocessor:", cc['command']['compiler'])

        # Язык
        pvs_studio_args.append('--language=' + PVS.map_lang(cc['source_metadata']['lang']))

        # Стандарт
        if (std := PVS.map_std(cc['source_metadata']['standard'])):
            pvs_studio_args.append('--std=' + std)

        # ----
        # ----
        # Формирование имени выходного файла
        result_file_parts = [*Path(cc['preprocessed_file']).parts][1:]
        result_file_parts[-1] = os.path.splitext(result_file_parts[-1])[0]+'.PVS-Studio.log'
        result_file = Path(*result_file_parts)

        # Создание каталога выходного файла
        real_result_file_on_host = self.__result_dir / result_file
        real_result_file_on_host.parent.mkdir(mode=0o755, parents=True, exist_ok=True)

        # --

        # Добавление исходного препроцессированного и выходного файла
        pvs_studio_args.extend([ '--source-file', cc['source_file']                             ])
        pvs_studio_args.extend([ '--i-file'     , Path('/pvs')        / cc['preprocessed_file'] ])
        pvs_studio_args.extend([ '--output-file', Path('/pvs/result') / result_file             ])

        # ----
        # ----

        # ----
        # Каталог cc['command']['cwd'] надо создавать, так как система сборки может использовать
        # отдельные рабочие каталоги для объектных файлов и полные пути к исходникам.
        # В этом случае этот каталог не скопируется.
        real_cwd_on_host = (self.__source_dir / 'root' / Path(*Path(cc['command']['cwd']).parts[1:]))
        real_cwd_on_host.mkdir(mode=0o755, parents=True, exist_ok=True)

        command = [
            'chroot', self.__source_dir / 'root',
            '/pvs/bin/chexec', cc['command']['cwd'],

            '/pvs/bin/pvs-studio',
        ] + pvs_studio_args + self.__pvs_studio_external_args


        # --------------
        # Запуск команды

        self.__print("PVS сommand:", self.__mask_lic_info(command))
        self.__print("PVS-START-------")

        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        for line in proc.stdout:
            self.__print(line.rstrip())
        proc.wait()

        self.__print("PVS-END---------")
        self.__print("PVS exit code:", proc.returncode)

        # --------------
        # Конец
        self.__print("ANALYSIS-END--------------------")

        # --------------



if __name__ == '__main__':
    app = BuildTraceAnalyzerPVS()
    app.main()
