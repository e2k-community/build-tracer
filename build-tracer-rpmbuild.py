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
#
# Настройка параметров через переменные окружения:
#
#   BUILD_TRACER_OUTPUT_DIR - каталог сохранения результатов трассировки.
#
#   BUILD_TRACER_PARALLEL - количество потоков запуска препроцессора.
#       Если не определена, то RPM_BUILD_NCPUS, иначе 1.
#       На большом проекте рекомендуется уменьшить для сокращения потребления памяти.
#
#
# Дополнительные настройки:
#
#   BUILD_TRACER_STAGE
#       Выполняемые стадии трассировки через ','
#       Допустимые варианты: rpmbuild,preprocessing или 'all' (по умолчанию) - эквивалент 'rpmbuild,preprocessing'
#
#   BUILD_TRACER_SRPM_NAME
#       Имя RPM-пакета. Используется при повторном парсинге, если стадия rpmbuild пропущена.
#
# --------------------------------------------------------------

import copy
import hashlib
import io
import itertools
import json
import multiprocessing
import os
import os.path
import re
import shutil
import subprocess
import sys
import time

from collections import Counter
from enum        import Enum
from pathlib     import Path
from typing      import Any, Final

import dataclasses
from dataclasses import dataclass



# --------------------------------------------------------------
# Системные параметры
#

class Config:
    strace_command   : Final[Path] = os.environ.get('BUILD_TRACER_STRACE_COMMAND'  , '/usr/bin/strace'  )
    rpmbuild_command : Final[Path] = os.environ.get('BUILD_TRACER_RPMBUILD_COMMAND', '/usr/bin/rpmbuild')



# --------------------------------------------------------------
# Параметры языков
#

class Language:
    class FileType(Enum):
        SOURCE = 1
        HEADER = 2
        MODULE = 3

    class ID(str, Enum):
        C   = 'c'
        CXX = 'c++'


    # ВНИМАНИЕ: Расширения начинаются с точки!
    # Список ожидаемых расширений файлов с исходными кодами
    source_exts : Final[dict] = {
        # C:
        '.c'   : (FileType.SOURCE, (ID.C,)),

        '.h'   : (FileType.HEADER, (ID.C, ID.CXX,)),

        # C++:
        '.C'   : (FileType.SOURCE, (ID.CXX,)),
        '.c++' : (FileType.SOURCE, (ID.CXX,)),
        '.cc'  : (FileType.SOURCE, (ID.CXX,)),
        '.cpp' : (FileType.SOURCE, (ID.CXX,)),
        '.cxx' : (FileType.SOURCE, (ID.CXX,)),

        '.H'   : (FileType.HEADER, (ID.CXX,)),
        '.h++' : (FileType.HEADER, (ID.CXX,)),
        '.hh'  : (FileType.HEADER, (ID.CXX,)),
        '.hpp' : (FileType.HEADER, (ID.CXX,)),
        '.hxx' : (FileType.HEADER, (ID.CXX,)),
        '.ipp' : (FileType.HEADER, (ID.CXX,)), # Boost

        # Модули С++:
        '.cppm': (FileType.MODULE, (ID.CXX,)),
        '.ixx' : (FileType.MODULE, (ID.CXX,)),
    }



# --------------------------------------------------------------
# Компиляторы
#

# Идентификатор компилятора из матчера
@dataclass(eq=True, frozen=True)
class CompilerId:
    id     : str
    like   : str
    version: str
    def ids(self):
        if self.like is None:
            return (self.id,     )
        else:
            return (self.id, self.like)

    def to_json_dict(self):
        if self.like is None:
            return { 'id': self.id,                    'version': self.version }
        else:
            return { 'id': self.id, 'like': self.like, 'version': self.version }


@dataclass
class CompilerCommand:
    cwd       : Path
    compiler  : CompilerId
    executable: str
    args      : list[str]

    # Только для счетчиков
    def as_tuple(self):
        return (self.cwd, self.compiler, self.executable, tuple(e for e in self.args))


# Метаданные файла исходного кода
@dataclass(eq=True, frozen=True)
class SourceFileCompilerMetadata:
    lang    : Language.ID
    standard: str


class CompilerMatcher:
    # Стандарты по умолчанию
    __std_default = {
        #
        # MCST LCC
        #
        'lcc' : [
            (    None, { Language.ID.C: 'gnu18', Language.ID.CXX: 'gnu++14', }), # <1.28
            ("1.28.0", { Language.ID.C: 'gnu18', Language.ID.CXX: 'gnu++17', }), #  1.28,1.29,...
        ],

        #
        # GCC
        #
        # https://gcc.gnu.org/onlinedocs/gcc-11.1.0/gcc/Standards.html
        'gcc' : [
            (    None, { Language.ID.C: 'gnu90', Language.ID.CXX: 'gnu++98', }), # <5.0.0
            ( "5.0.0", { Language.ID.C: 'gnu11', Language.ID.CXX: 'gnu++98', }), #  5.0.0+
            ( "6.0.0", { Language.ID.C: 'gnu11', Language.ID.CXX: 'gnu++14', }), #  6.0.0+
            ("11.0.0", { Language.ID.C: 'gnu17', Language.ID.CXX: 'gnu++17', }), # 11.0.0+
            ("15.0.0", { Language.ID.C: 'gnu23', Language.ID.CXX: 'gnu++17', }), # 15.0.0+
        ],

        #
        # Clang
        #
        # https://clang.llvm.org/docs/CommandGuide/clang.html
        # https://stackoverflow.com/questions/75679555/how-can-i-find-the-default-version-of-the-c-language-standard-used-by-my-compi
        # https://releases.llvm.org/16.0.0/tools/clang/docs/ReleaseNotes.html#potentially-breaking-changes
        # https://releases.llvm.org/6.0.1/tools/clang/docs/ReleaseNotes.html#c-language-changes-in-clang
        #
        # C  : https://godbolt.org/z/dKc6jWxMG
        # C++: https://godbolt.org/z/Gb8Th5Wsv
        'clang' : [
            (    None, { Language.ID.C: 'gnu99', Language.ID.CXX: 'gnu++98', }), # <3.6.0
            ( "3.6.0", { Language.ID.C: 'gnu11', Language.ID.CXX: 'gnu++98', }), #  3.6.0+
            ( "6.0.0", { Language.ID.C: 'gnu11', Language.ID.CXX: 'gnu++14', }), #  6.0.0+
            ("11.0.0", { Language.ID.C: 'gnu17', Language.ID.CXX: 'gnu++14', }), # 11.0.0+
            ("16.0.0", { Language.ID.C: 'gnu17', Language.ID.CXX: 'gnu++17', }), # 16.0.0+
        ],
    }

    __std_ansi = {
        #
        # MCST LCC
        #
        'lcc' : {
            Language.ID.C   : 'c89',
            Language.ID.CXX : 'c++98',
        },

        #
        # GCC
        #
        # https://gcc.gnu.org/onlinedocs/gcc-11.1.0/gcc/Standards.html
        'gcc' : {
            Language.ID.C   : 'c90',
            Language.ID.CXX : 'c++98',
        },

        #
        # Clang
        #
        # https://clang.llvm.org/docs/CommandGuide/clang.html
        'clang' : {
            Language.ID.C   : 'c89',
        },
    }

    @staticmethod
    def version_compare(a, b):
        versiontuple = lambda v: tuple(map(int, (v.split("."))))
        at = versiontuple(a)
        bt = versiontuple(b)
        at_l = len(at)
        bt_l = len(bt)
        m_l = max(at_l, bt_l)
        at += tuple([0]*(m_l - at_l))
        bt += tuple([0]*(m_l - bt_l))
        if at == bt:
            return 0
        elif  at < bt:
            return -1
        else:
            return 1


    # Получение стандарта по умолчанию для заданного компилятора.
    @staticmethod
    def get_default_std(compiler: CompilerId, lang : Language.ID) -> str:
        data = CompilerMatcher.__std_default[compiler.id]
        i = len(data)-1
        while data[i][0]:
            c = CompilerMatcher.version_compare(data[i][0], compiler.version)
            if c <= 0:
                return data[i][1][lang]
            i -= 1
        return data[0][1][lang]


    # Получение стандарта для опции -ansi для заданного компилятора.
    #   Может вернуть None, если не поддерживается параметр -ansi.
    @staticmethod
    def get_ansi_std(compiler: CompilerId, lang : Language.ID):
        return CompilerMatcher.__std_ansi[compiler.id].get(lang)



    # Извлечение идентификатора  и версии компилятора
    __prefilter_by_path_regex = [
        # lcc:
        re.compile(r"/(?:[^/]+\-)?(?:(?:lcc)|(?:l\+\+))$"                                       ),

        # clang:
        re.compile(r"/(?:[^/]+\-)?(?:(?:clang)|(?:clang\+\+))(?:(:?-\d+)(:?.\d+)*)?$"           ),

        # gcc -> lcc:
        re.compile(r"/(?:[^/]+\-)?(?:(?:cc)|(?:gcc)|(?:c\+\+)|(?:g\+\+))(?:(:?-\d+)(:?.\d+)*)?$"),
    ]
    __id_version_ncache = set()
    __id_version_cache = dict()
    __id_version_regex = [
        # lcc:
        #   lcc:1.27.14:Jan-31-2024:e2k-v5-linux
        (re.compile(r"^(?:(?:lcc)|(?:l\+\+)):(?P<version>(?:\d+)\.(?:\d+)\.(?:\d+))"                 ), lambda ver: CompilerId('lcc'  , 'gcc', ver)),

        # clang:
        #   clang version 3.9.1 (tags/RELEASE_391/final 296768)
        #   clang version 10.0.1 (https://github.com/llvm/llvm-project.git ef32c611aa214dea855364efd7ba451ec5ec3f74)
        #   clang version 19.1.0 (https://github.com/llvm/llvm-project.git a4bf6cd7cfb1a1421ba92bca9d017b49936c55e4)
        #   clang version 19.1.7 (CentOS 19.1.7-1.el9)
        #   Ubuntu clang version 20.1.2 (0ubuntu1)
        #   Ubuntu clang version 20.1.2 (0ubuntu1)
        #   Ubuntu clang version 20.1.2 (0ubuntu1)
        (re.compile(r"(?:(?:clang)|(?:clang\+\+)) version (?P<version>(?:\d+)\.(?:\d+)\.(?:\d+))"    ), lambda ver: CompilerId('clang', None , ver)),

        # gcc (последний):
        #   g++ (Compiler-Explorer-Build-gcc--binutils-2.40) 13.2.0
        #   g++ (GCC) 11.5.0 20240719 (Red Hat 11.5.0-5)
        #   g++ (GCC-Explorer-Build) 4.9.3
        #   g++-15 (Ubuntu 15-20250404-0ubuntu1) 15.0.1 20250404 (experimental) [master r15-9193-g08e803aa9be]
        #   gcc (GCC) 11.5.0 20240719 (Red Hat 11.5.0-5)
        #   gcc-14 (Ubuntu 14.2.0-19ubuntu2) 14.2.0
        #   gcc-15 (Ubuntu 15-20250404-0ubuntu1) 15.0.1 20250404 (experimental) [master r15-9193-g08e803aa9be]
        (re.compile(r"^(?:(?:gcc)|(?:g\+\+))(?:.*?) \([^)]+\) (?P<version>(?:\d+)\.(?:\d+)\.(?:\d+))"), lambda ver: CompilerId('gcc'  , None , ver)),
    ]

    # Получение идентификатора и версии компилятора
    def match(self, path : str, args) -> CompilerId:
        id_tuple = (path, args[0])

        # Если есть в негативном кеше
        if id_tuple in CompilerMatcher.__id_version_ncache:
            return None

        if (cid := CompilerMatcher.__id_version_cache.get(id_tuple)):
            return cid

        if not any( r.search(path) for r in CompilerMatcher.__prefilter_by_path_regex ):
            CompilerMatcher.__id_version_ncache.add(id_tuple)
            return None

        #if not os.path.exists(path):
        #    return None
        try:
            ret = subprocess.run([args[0], '--version'], executable=path, capture_output=True, text=True, env={ "LC_ALL": "C", "LANG": "C" })
        except Exception as e:
            # FileNotFoundError и другие
            CompilerMatcher.__id_version_ncache.add(id_tuple)
            return None

        if ret.returncode != 0:
            CompilerMatcher.__id_version_ncache.add(id_tuple)
            return None

        stdout_lines = ret.stdout.splitlines()
        for r in CompilerMatcher.__id_version_regex:
            if (m := r[0].search(stdout_lines[0])):
                cid = r[1](m['version'])
                CompilerMatcher.__id_version_cache[id_tuple] = cid
                return cid

        CompilerMatcher.__id_version_ncache.add(id_tuple)
        return None


    # TODO: Вычислять реальные параметры компиляторов (сейчас берутся все параметры с подходящим расширением)
    def get_sources_from_args(self, cc_command : CompilerCommand):
        sources_in_args = list()
        for arg in cc_command.args:
            ext = os.path.splitext(arg)[1]
            meta = Language.source_exts.get(ext)
            if meta is not None:
                if meta[0] == Language.FileType.SOURCE:
                    sources_in_args.append(arg)
        return sources_in_args


    def get_source_metadata(self, cc_command : CompilerCommand, source):
        # Извлечение стандарта
        #   gcc,lcc: -std=<value>
        #   clang  : -std=<arg>, --std=<arg>, --std <arg>
        std      = None
        std_lang = None
        for i, arg in enumerate(cc_command.args):
            if (m := re.match(r"^-?-std=(?P<std>.*)$", arg)):
                std = m['std']
                break
            elif arg == '--std':
                std = cc_command.args[i+1]
                break
        if std is not None:
            if '++' in std:
                std_lang = Language.ID.CXX
            else:
                std_lang = Language.ID.C

        # Язык фронтенда
        frontend_lang = None;
        if '++' in cc_command.args[0]:
            frontend_lang = Language.ID.CXX
        else:
            frontend_lang = Language.ID.C

        # Язык файла
        ext_meta = Language.source_exts[ os.path.splitext(source)[1] ]
        file_lang = ext_meta[1][0]
        assert ext_meta[0] == Language.FileType.SOURCE

        # g++, clang++(ошибки), l++ - ВСЕГДА С++, стандарт С в std - ошибка и его игнорирование.
        # 
        # gcc, clang(ошибки), lcc:
        #                1.c   - C
        #                1.cpp - C++
        #     -std=c99   1.c   - C
        #     -std=c99   1.cpp - C++, стандарт С   в std - ошибка и его игнорирование.
        #     -std=c++11 1.c   - C  , стандарт С++ в std - ошибка и его игнорирование.
        #     -std=c++11 1.cpp - C++

        ret_lang = file_lang # По умолчанию, как у файла
        ret_std  = std       # По умолчанию, как у аргумента стандарта

        if frontend_lang == Language.ID.CXX:
            ret_lang = Language.ID.CXX
        if std_lang is not None and (std_lang != ret_lang):
            ret_std = None # Сброс в дефолт, игнор

        # Обработка -ansi
        if ret_std is None and std is None and '-ansi' in cc_command.args:
            ret_std = CompilerMatcher.get_ansi_std(cc_command.compiler, ret_lang)

        # Выбор std по умолчанию
        if ret_std is None:
            ret_std = CompilerMatcher.get_default_std(cc_command.compiler, ret_lang)

        return SourceFileCompilerMetadata(ret_lang, ret_std)


    def make_preprocessor_command(self, cc_command, preprocessed_result_file, source_idx, sources_in_args):
        # Полное рекурсивное копирование всех структур
        preprocessed_command = copy.deepcopy(cc_command)

        # Удаляем исходные файлы из аргументов
        preprocessed_command.args = [ arg for arg in preprocessed_command.args if arg not in sources_in_args ]

        # Заменяем вывод на препроцессированный вывод
        o_idx = None
        try:
            o_idx = preprocessed_command.args.index('-o')
        except ValueError:
            pass
        unknown_preprocessor = True
        for id in preprocessed_command.compiler.ids():
            if id is None:
                continue
            if id in ['lcc', 'gcc', 'clang']:
                if '-E' in preprocessed_command.args:
                    raise ValueError("-E already present in args")
                e_args = ['-E', '-o', str(preprocessed_result_file)]
                if o_idx is not None:
                    preprocessed_command.args[o_idx:o_idx+2] = e_args
                else:
                    # Не задан выходной файл - дописываем аргументы после имени компилятора
                    preprocessed_command.args[1:1] = e_args
                preprocessed_command.args.append(sources_in_args[source_idx])
                unknown_preprocessor = False
                break

        assert (unknown_preprocessor == False), "unknown compiler, inconsistency CompilerMatcher"

        return preprocessed_command



# --------------------------------------------------------------
# Фильтр файлов для копирования
#

class OpenFilesFilter:

    # Список запрещенных для копирования расширений в виде регулярных выражений.
    ignore_ext_regex : Final[list] = [
        # so-библиотеки с версиями.
        r"\.so(?:\.\d+)+$",
    ]
    # Список запрещенных для копирования расширений.
    #   ВНИМАНИЕ: Расширения начинаются с точки.
    ignore_ext_list : Final[list] = [
        '.a' , '.o' , '.s' , '.so',
    ]

    # Список полных путей к файлам, запрещенных для копирования.
    #   Сравниваются на равенство.
    ignore_file_list : Final[frozenset] = frozenset([
        '/etc/localtime',
        '/etc/ld.so.cache',
    ])

    # Список каталогов, запрещенных для копирования.
    #   ВНИМАНИЕ: Без "/" в конце.
    ignore_dir_list : Final[list] = [
        '/dev',
        '/etc',
        '/proc',
        '/run',
        '/sys',
        '/usr/lib/rpm',
        '/usr/lib64/gconv',
        '/usr/lib/locale',
        '/usr/share/locale',
        '/usr/share/zoneinfo',
    ]


    def __init__(self):
        self.__ignore_regex = re.compile('|'.join(
            map(lambda s: '(?:' + s + ')', itertools.chain(
                # Список регулярных выражений сложных расширений
                OpenFilesFilter.ignore_ext_regex,

                # Список расширений
                map(lambda e: re.escape(e) + '$', OpenFilesFilter.ignore_ext_list),

                # Список каталогов
                map(lambda d: '^' + re.escape(d) + '(?:$|/)', OpenFilesFilter.ignore_dir_list)
            ))
        ))


    def allow(self, path : Path) -> bool:
        path_str = str(path)
        # Путь в списке запрещенных файлов
        if path_str in OpenFilesFilter.ignore_file_list:
            return False
        # Путь в списке запрещенных по регулярным выражениям
        if self.__ignore_regex.search(path_str):
            return False
        return True



# --------------------------------------------------------------
# Статистика работы
#

class Timer:
    def __init__(self):
        self.stages = []
        self.cut('') # запись времени начала работы

    def cut(self, name):
        self.stages.append((name, time.time()))

    def __format_summary_row(self, name, interval):
        return "{}: {:.3f}s".format(name, interval)

    def get_summary_pretty(self):
        ret = []
        l = len(self.stages)
        for i in range(1, l):
            ret.append(self.__format_summary_row(self.stages[i][0], (self.stages[i][1] - self.stages[i - 1][1])))
        ret.append(self.__format_summary_row("TOTAL", (self.stages[l - 1][1] - self.stages[0][1])))
        return ret



# --------------------------------------------------------------
#
#

@dataclass
class SysCallError:
    errno : int
    errstr: str

@dataclass
class SysCallEntity:
    ts          : float
    name        : str
    returnvalue : int
    returnfile  : str = None
    error       : SysCallError = None
    args_raw    : str = None
    args        : list[Any] = dataclasses.field(default_factory=list)



@dataclass
class ProcTrace:
    pid     : int
    cwd     : Path   = None
    ts_start: float  = None
    ts_end  : float  = None
    exitcode: int    = None
    killedby: str    = None
    syscall : list[SysCallEntity] = dataclasses.field(default_factory=list)


#
# Фильтр системных вызовов, которые сохраняются в памяти при парсинге.
#
class SysCallFilter:
    def __init__(self):
        pass

    def allow(self, syscall : SysCallEntity):
        # Игнорирование неуспешных syscall
        if syscall.returnvalue < 0:
            return False
        return True


class StraceParser:
    regex_line = re.compile(r"^(?P<timestamp>\d+\.\d+)\s(?:(?:\+\+\+ killed by (?P<killedby>[A-Z]+) (?:\(core dumped\) )?\+\+\+)|(?:\+\+\+ exited with (?P<exitcode>-?\d+) \+\+\+)|(?:(?P<syscall>(?:chdir)|(?:fork)|(?:vfork)|(?:clone)|(?:clone2)|(?:clone3)|(?:execve)|(?:execveat)|(?:fchdir)|(?:open)|(?:openat)|(?:openat2))\((?P<args>.*)\)(?:\s+)=(?:\s+)(?:(?P<returnvalue>\-?\d+)(?:(?:<(?P<returnfile>.*)>)|(?: (?P<errno>[A-Z]+) \((?P<errstr>.*)\)))?)))$")

    #regex_syscall_clone_args   = re.compile()
    regex_syscall_execve_args  = re.compile(r"^\"(?P<path>(\\x[0-9A-Fa-f]{2})*)\", \[(?P<argv>\"(?:(\\x[0-9A-Fa-f]{2})*)\"(?:, \"(?:(\\x[0-9A-Fa-f]{2})*)\")*)(?:\.\.\.)?\], (?:(?:\[(?P<env>\"(?:(\\x[0-9A-Fa-f]{2})*)\"(?:, \"(?:(\\x[0-9A-Fa-f]{2})*)\")*)(?:\.\.\.)?\])|(?P<envph>0x[0-9a-fA-F]+ /\* [\d]+ vars \*/))$")
    regex_syscall_chdir_args   = re.compile(r"^\"(?P<path>(\\x[0-9A-Fa-f]{2})*)\"$")
    regex_syscall_fchdir_args  = re.compile(r"^(?P<fd>\d+)<(?P<path>(\\x[0-9A-Fa-f]{2})*)>$")
    regex_syscall_open_args    = re.compile(r"^\"(?P<path>(\\x[0-9A-Fa-f]{2})*)\", (?P<oflag>O_[A-Z]+(?:\|O_[A-Z]+)*)(?:, (?P<mode>\d+))?$")
    regex_syscall_openat_args  = re.compile(r"^(?P<cwdfd>(?:(?:\d+)|(?:AT_FDCWD)))(?:<(?P<cwd>.*)>)?, \"(?P<path>(\\x[0-9A-Fa-f]{2})*)\", (?P<oflag>O_[A-Z]+(?:\|O_[A-Z]+)*)(?:, (?P<mode>\d+))?$")
    regex_syscall_openat2_args = re.compile(r"^(?P<cwdfd>(?:(?:\d+)|(?:AT_FDCWD)))(?:<(?P<cwd>.*)>)?, \"(?P<path>(\\x[0-9A-Fa-f]{2})*)\", {(?P<how>[^}]*?)}, (?P<size>\d+)$")


    def __init__(self, syscall_filter):
        self.__syscall_filter : SysCallFilter = syscall_filter;


    def __decode_xstr(self, raw : str) -> str:
        # Декодер строк в экранированном формате strace
        if raw is None:
            return None
        return raw.encode('latin1', 'backslashreplace').decode('unicode-escape')

    def __decode_argv_env(self, raw : str) -> list[str]:
        if raw is None:
            return None
        l = raw.split(",")
        for i in range(len(l)):
            l[i] = self.__decode_xstr(l[i].strip().strip('"'))
        return l

    def parse_file(self, pid: int, path : Path) -> ProcTrace:
        with path.open() as file:
            return self.parse_fd(pid, file)

    def parse_fd(self, pid: int, fd : io.TextIOBase) -> ProcTrace:
        proc = ProcTrace( pid )
        for line in fd:
            line = line.rstrip()
            m = StraceParser.regex_line.match(line)
            if m is None:
                continue

            v_timestamp = float(m.group('timestamp'))

            proc.ts_start = v_timestamp if proc.ts_start is None else min(proc.ts_start, v_timestamp)
            proc.ts_end   = v_timestamp if proc.ts_end   is None else max(proc.ts_end  , v_timestamp)

            v_killedby  = m.group('killedby')
            if v_killedby is not None:
                proc.killedby = v_killedby
                continue

            v_exitcode  = m.group('exitcode')
            if v_exitcode is not None:
                proc.exitcode = int(v_exitcode)
                continue

            v_syscall     = m.group('syscall')
            v_args        = m.group('args')
            v_returnvalue = m.group('returnvalue')
            v_returnfile  = m.group('returnfile')
            v_errno       = m.group('errno')
            v_errstr      = m.group('errstr')

            if v_syscall is not None:
                syscall = SysCallEntity(v_timestamp, v_syscall, int(v_returnvalue))

#                print("syscall:", syscall.name, ":", syscall.returnvalue)

                if v_returnfile is not None:
                    syscall.returnfile = self.__decode_xstr(v_returnfile)
#                    print(syscall.name, ":", syscall.returnfile)

                if v_errno is not None:
                    syscall.error = SysCallError(v_errno, v_errstr)


                # ПАРСИНГ АРГУМЕНТОВ
                if v_args is not None:
                    args_raw = v_args.strip();

                    if syscall.name == 'fork':
                        # Для реальной работы требуется только возвращаемое значение
                        # Параметров НЕТ
#                        print("fork:", args_raw, v_returnvalue)
                        pass
                    elif syscall.name == 'vfork':
                        # Для реальной работы требуется только возвращаемое значение
                        # Параметров НЕТ
#                        print("vfork:", args_raw, v_returnvalue)
                        pass
                    elif syscall.name == 'clone':
                        # Для реальной работы требуется только возвращаемое значение
                        # Сохраняем оригинальные параметры, так как не парсим их
                        syscall.args_raw = args_raw
#                        print("clone:", args_raw, v_returnvalue)
                        pass
                    elif syscall.name == 'clone2':
                        # Для реальной работы требуется только возвращаемое значение
                        # Сохраняем оригинальные параметры, так как не парсим их
                        syscall.args_raw = args_raw
#                        print("clone2:", args_raw, v_returnvalue)
                        pass
                    elif syscall.name == 'clone3':
                        # Для реальной работы требуется только возвращаемое значение
                        # Сохраняем оригинальные параметры, так как не парсим их
                        syscall.args_raw = args_raw
#                        print("clone3:", args_raw, v_returnvalue)
                        pass
                    elif syscall.name == 'execve':
                        am = StraceParser.regex_syscall_execve_args.match(args_raw)

                        av_path  = self.__decode_xstr(am.group('path'))
                        av_argv  = self.__decode_argv_env(am.group('argv'))
                        av_env   = self.__decode_argv_env(am.group('env'))
                        av_envph = am.group('envph')

                        syscall.args.append(av_path)
                        syscall.args.append(av_argv)
                        syscall.args.append(av_env if av_env is not None else av_envph)

#                        print("execve:", syscall.args)
#                        print("execve:", args_raw, syscall.args)
#                        print("execve:", args_raw)

                    elif syscall.name == 'execveat':
                        # TODO: Реализовать обработку execveat
                        raise NotImplementedError('Implement syscall parsing: execveat')

                    elif syscall.name == 'chdir':
                        am = StraceParser.regex_syscall_chdir_args.match(args_raw)
                        av_path  = self.__decode_xstr(am.group('path'))
                        syscall.args.append(av_path)

#                        print("chdir:", args_raw, syscall.args)

                    elif syscall.name == 'fchdir':
                        am = StraceParser.regex_syscall_fchdir_args.match(args_raw)
                        av_fd    = int(am.group('fd'))
                        av_path  = self.__decode_xstr(am.group('path'))
                        syscall.args.append( (av_fd, av_path) )

#                        print("fchdir:", args_raw, syscall.args)

                    elif syscall.name == 'open':
                        am = StraceParser.regex_syscall_open_args.match(args_raw)
                        av_path  = self.__decode_xstr(am.group('path'))
                        av_oflag = am.group('oflag')
                        av_mode  = am.group('mode')
                        syscall.args.append(av_path)
                        syscall.args.append(av_oflag)
                        if av_mode is not None:
                            syscall.args.append(av_mode)

#                        print("open:", args_raw, syscall.args)

                    elif syscall.name == 'openat':
                        am = StraceParser.regex_syscall_openat_args.match(args_raw)
                        av_cwdfd = am.group('cwdfd')
                        av_cwd   = self.__decode_xstr(am.group('cwd'))
                        av_path  = self.__decode_xstr(am.group('path'))
                        av_oflag = am.group('oflag')
                        av_mode  = am.group('mode')

                        # AT_FDCWD
                        syscall.args.append((av_cwdfd, av_cwd))
                        syscall.args.append(av_path)
                        syscall.args.append(av_oflag)
                        if av_mode is not None:
                            syscall.args.append(av_mode)

#                        print("openat:", args_raw, syscall.args)

                    elif syscall.name == 'openat2':
                        am = StraceParser.regex_syscall_openat2_args.match(args_raw)
                        av_cwdfd = am.group('cwdfd')
                        av_cwd   = self.__decode_xstr(am.group('cwd'))
                        av_path  = self.__decode_xstr(am.group('path'))
                        av_how   = am.group('how')
                        av_size  = am.group('size')

                        how = dict()
                        if av_how is not None:
                            for a in av_how.split(", "):
                                kv = a.split("=")
                                how[kv[0]] = kv[1]

                        # AT_FDCWD
                        syscall.args.append((av_cwdfd, av_cwd))
                        syscall.args.append(av_path)
                        syscall.args.append(how)
                        syscall.args.append(av_size)

#                        print("openat2:", args_raw, syscall.args)
                    else:
                        # Для неизвестного системного вызова сохраняем оригинальные параметры
                        syscall.args_raw = args_raw

                # ----

                # Добавляем только если фильтр прошли:
                if self.__syscall_filter.allow(syscall):
                    proc.syscall.append(syscall)

#            print("ts:", v_timestamp, type(v_timestamp))
#            print("exitcode:", v_exitcode)
#            print("syscall:" , v_syscall)

        proc.syscall.sort(key=lambda v: v.ts)
        return proc



# ------------------------------
# Обработка strace процессов
#

class StraceData:
    def __init__(self, syscall_filter: SysCallFilter, root_cwd : Path, files, parallel):
        self.root_cwd : Path                 = root_cwd;
        self.proc_map : dict[int, ProcTrace] = { }
        self.root_pid : int                  = None

        self.__syscall_filter: SysCallFilter = syscall_filter
        self.__strace_parser : StraceParser  = StraceParser(self.__syscall_filter)
        self.__parallel      : int           = parallel

        self.__run(files)

    def __pid_from_path(self, path : Path) -> int:
        return int(path.name.split('.', 1)[1])

    def do_file(self, path):
        pid = self.__pid_from_path(path)
        return self.__strace_parser.parse_file(pid, path)

    def __run(self, files):
        if self.__parallel > 1:
            with multiprocessing.Pool(processes=self.__parallel) as pool:
                results = pool.map(self.do_file, files)
            for trace in results:
                self.proc_map[trace.pid] = trace
        else:
            for path in files:
                trace = self.do_file(path)
                self.proc_map[trace.pid] = trace

        if len(self.proc_map) == 0:
            raise ValueError("empty input file list")

        # Получение корневого процесса, у него самый маленький ts_start
        self.root_pid = min(self.proc_map.values(), key=lambda v: v.ts_start).pid



@dataclass
class CompilerCall:
    pid       : int
    exitcode  : int
    command   : CompilerCommand
    open_files: list[Any] = dataclasses.field(default_factory=list)


class CompilerExtractor:
    __fork_syscall_set = frozenset([ 'fork', 'vfork', 'clone', 'clone2', 'clone3' ])

    def __init__(self, strace_data : StraceData, compiler_matcher : CompilerMatcher):
        self.__strace_data     : StraceData         = strace_data
        self.__compiler_matcher: CompilerMatcher    = compiler_matcher
        self.__compiler_calls  : list[CompilerCall] = []

        # Бежим по дереву начиная с корня. Как встретили exec меняем параметры процесса,
        # По clone - идем рекурсивным вызовом с текущим cwd.
        self.__walk_proc(self.__strace_data.root_pid, self.__strace_data.root_cwd, False, 0)
        pass

    def compiler_calls(self):
        return self.__compiler_calls 

    def __walk_proc(self, pid : int, cwd : Path, is_compiler_internals : bool, level : int):
#        strprefix : Final = '-' * (level+1)
#        print(strprefix, "proc:", pid, "cwd:", cwd, type(cwd))

        proc = self.__strace_data.proc_map[pid]

        compiler_call : CompilerCall = None
        open_files    : list[Any]    = [];

        for sc in proc.syscall:
            if sc.name in CompilerExtractor.__fork_syscall_set:
                next_pid : int = sc.returnvalue
                if next_pid in self.__strace_data.proc_map:
                    open_files = open_files + self.__walk_proc(next_pid, cwd, is_compiler_internals, level + 1);
            elif sc.name == 'fchdir':
                new_cwd = cwd / Path(sc.args[0][1])
#                print(strprefix, "proc:", pid, "cwd:", cwd, "fchdir", sc.args[0][1], new_cwd)
                cwd = new_cwd
            elif sc.name == 'chdir':
                new_cwd = cwd / Path(sc.args[0])
#                print(strprefix, "proc:", pid, "cwd:", cwd, "chdir", sc.args[0], new_cwd)
                cwd = new_cwd
            elif sc.name == 'execve':
#                # Проверка на соответствие пути в ENV. Не гарантируется его наличие.
#                env = sc.args[2]
#                if isinstance(env, list):
#                    for e in env:
#                        if e.startswith('PWD='):
#                            env_pwd = Path(e.removeprefix('PWD='))
#                            if env_pwd != cwd:
#                                print("ERROR: invalid cwd '{}' != '{}'".format(env_pwd, cwd))

                # НА ПУТИ ВНИЗ по дереву ловим только первый компилятор и все открытые в дочерних процессах файлы складываем к нему.
                # Нам не важно, что там компилятор вызывает внутри, нам важны открытые файлы.
                if is_compiler_internals == False:
                    executable = sc.args[0]
                    compiler_id = self.__compiler_matcher.match(executable, sc.args[1])
                    if compiler_id is not None:
                        is_compiler_internals = True
                        compiler_call = CompilerCall(proc.pid, proc.exitcode, CompilerCommand(cwd, compiler_id, executable, copy.deepcopy(sc.args[1])))
            elif sc.name == 'execveat':
                # TODO: Реализовать обработку execveat
                raise NotImplementedError('Implement syscall processing: execveat')
            elif sc.name == 'open':
                if is_compiler_internals:
                    # Только существующие файлы, которые получилось открыть.
                    if sc.returnvalue >= 0:
                        open_files.append( (cwd, sc.args) )

            elif sc.name == 'openat':
                if is_compiler_internals:
                    # Только существующие файлы, которые получилось открыть.
                    if sc.returnvalue >= 0:
                        sc_cwd = sc.args[0][1]
                        if sc_cwd is None:
                            sc_cwd = cwd
                        open_files.append( (Path(sc_cwd), sc.args[1:]) )

            elif sc.name == 'openat2':
                if is_compiler_internals:
                    # Только существующие файлы, которые получилось открыть.
                    if sc.returnvalue >= 0:
                        sc_cwd = sc.args[0][1]
                        if sc_cwd is None:
                            sc_cwd = cwd
                        open_files.append( (Path(sc_cwd), [ sc.args[1], sc.args[2]['flags'],  sc.args[2]['mode'] ]) )

        if compiler_call is not None:
            compiler_call.open_files = open_files
            self.__compiler_calls.append(compiler_call)
            open_files = list() # Нет смысла прокидывать список выше - обнуляем.

        return open_files



# ------------------------------
# Результат обработки
#


@dataclass
class ResultItem:
    preprocessed_file: Path
    source_file      : str
    source_metadata  : SourceFileCompilerMetadata
    command          : CompilerCommand


# Конвертация в json:
class ResultEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ResultItem):
            return obj.__dict__
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, CompilerCommand):
            return obj.__dict__
        if isinstance(obj, CompilerId):
            return obj.to_json_dict()
        if isinstance(obj, SourceFileCompilerMetadata):
            return obj.__dict__
        return json.JSONEncoder.default(self, obj)



# --------------------------------------------------------------
# Работа
#

class RpmbuildTracer:
    class Stages:
        def __init__(self, arg):
            lst = [s.strip() for s in arg.split(",")]
            for name in ['rpmbuild', 'preprocessing']:
                self.__setattr__(name, name in lst or arg == 'all')


    def __init__(self):
        # Таймер этапов
        self.timer = Timer()

        self.__stage     : Final[RpmbuildTracer.Stages] = RpmbuildTracer.Stages(os.environ.get('BUILD_TRACER_STAGE', 'all'))

        self.__output_dir: Final[Path] = Path(os.environ.get('BUILD_TRACER_OUTPUT_DIR', Path.cwd() / 'build_trace-{pid}'.format(pid=os.getpid())))
        self.__srpm_name : str         = None if self.__stage.rpmbuild else os.environ.get('BUILD_TRACER_SRPM_NAME')
        self.__parallel  : Final[int]  = max(int(os.environ.get('BUILD_TRACER_PARALLEL', os.environ.get('RPM_BUILD_NCPUS', '1'))), 1)

        self.__compiler_matcher  : CompilerMatcher = CompilerMatcher()
        self.__open_files_filter : OpenFilesFilter = OpenFilesFilter()

        # --------------
        # Таймер: отсечка инициализаци
        self.timer.cut('init')
        # --------------


    @property
    def output_dir(self):
        if self.__srpm_name:
            return self.__output_dir / self.__srpm_name
        else:
            return self.__output_dir


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


    def __print_summary(self):
        self.__print("SUMMARY-START-------------------")
        lines = self.timer.get_summary_pretty()
        for l in lines:
            self.__print(l)
        self.__print("SUMMARY-END---------------------")


    def __print_summary_exit(self, code : int):
        self.__print_summary()
        sys.exit(code)


    def main(self):
        # ------------------------------
        # Параметры командной строки для rpmbuild
        #

        rpmbuild_args = sys.argv[1:]


        # ------------------------------
        # Если rpmbuild не собирает бинарные пакеты (-ba, -bb),
        # то выполняем его как есть, без трассировки, и выходим
        #   https://github.com/rpm-software-management/mock/blob/mock-5.6-1/mock/py/mockbuild/backend.py#L696
        #   https://github.com/rpm-software-management/mock/blob/mock-5.6-1/mock/py/mockbuild/backend.py#L719
        #

        if not set(rpmbuild_args).intersection(['-ba', '-bb', '-ra', '-rb', '-ta', '-tb', '--rebuild', '--recompile']):
            rpmbuild_cmd_orig : Final[list[str]] = self.__make_rpmbuild_cmd_orig(rpmbuild_args)
            returncode = self.__exec_rpmbuild( rpmbuild_cmd_orig )
            sys.exit(returncode)


        # ------------------------------
        # Запуск rpmbuild
        #
        if self.__stage.rpmbuild:
            self.__print("RPMBUILD-START------------------")

            (rpmbuild_cwd, rpmbuild_returncode) = self.__do_rpmbuild(rpmbuild_args)

            self.__print("RPMBUILD-END--------------------")
            self.timer.cut('rpmbuild')
        else:
            # Чтение каталога запуска rpmbuild из файла
            with (self.output_dir / 'cwd').open() as f:
                rpmbuild_cwd = Path(f.read())
            # Чтение кода возврата rpmbuild из файла
            with (self.output_dir / 'rpmbuild.returncode').open() as f:
                rpmbuild_returncode = int(f.read())


        # ------------------------------
        # Если rpmbuild завершился с ошибкой, безусловно выходим.
        # Дальше делать нечего.

        if rpmbuild_returncode != 0:
            self.__print_summary_exit(rpmbuild_returncode)


        # ------------------------------
        # rpmbuild отработал, пакет собрался, трейсим компилятор.

        if self.__stage.preprocessing:
            self.__do_preprocessing(rpmbuild_cwd)


        # ------------------------------
        # Вывод общей информации о работе и завершение работы

        self.__print_summary_exit(0)


    # --------------------------------------------------------------
    # Запуск rpmbuild
    #
    def __make_rpmbuild_cmd_orig(self, rpmbuild_args):
        return [ Config.rpmbuild_command ] + rpmbuild_args


    def __get_rpmbuild_spec_path(self, rpmbuild_args):
        for arg in rpmbuild_args:
            if arg.endswith(".spec"):
                return arg
        return None


    def __do_rpmbuild(self, rpmbuild_args):
        # ------------------------------
        # Исходные параметры для rpmbuild
        #
        rpmbuild_cmd_orig : Final[list[str]] = self.__make_rpmbuild_cmd_orig(rpmbuild_args)
        cwd               : Final[Path]      = Path.cwd()

        # ------------------------------
        # Извлечение имени пакета
        # https://github.com/rpm-software-management/rpm/blob/a333eaa3f0fbce0c77cea23015144e13d9c039e2/docs/manual/tags.md?plain=1#L435
        #
        spec_file = self.__get_rpmbuild_spec_path(rpmbuild_args)
        if spec_file:
            ret = subprocess.run(['rpmspec', '-q', '--queryformat=%{nvr}', '--srpm', spec_file], capture_output=True, text=True, check=True)
            self.__srpm_name = ret.stdout.strip()

        # ------------------------------
        # Аргументы strace
        #
        strace_args : Final[list[str]] = [
            '-xx'       , # so strings are escaped
            '--absolute-timestamps=format:unix,precision:ns', # unix timestamp with nanosecond
            '-ff'       ,
            '--output={}/trace-rpmbuild'.format(self.output_dir / 'strace'),
            '--decode-fds=all'      ,
            '--string-limit={}'.format(os.sysconf('SC_ARG_MAX') if 'SC_ARG_MAX' in os.sysconf_names else 4194304), # 4194304 - максимум на e2k
            '--no-abbrev'           ,
            '-e', 'trace=fork,vfork,clone,?clone2,?clone3,execve,?execveat,chdir,fchdir,?open,?openat,?openat2',
            '-z', # print only syscalls that returned without an error code (--successful-only)
            '--seccomp-bpf',
        ]


        # ------------------------------
        # Подготовка и запуск rpmbuild


        # --------------
        # Преобразование аргументов для rpmbuild
        #

        rpmbuild_args_strace = list(rpmbuild_args)

        # Обработка --clean / --noclean
        #
        if '--clean' in rpmbuild_args_strace:
            rpmbuild_args_strace = ['--noclean' if '--clean' == x else x for x in rpmbuild_args_strace]
        if '--noclean' not in rpmbuild_args_strace:
            rpmbuild_args_strace = [ '--noclean' ] + rpmbuild_args_strace


        # --------------
        # Полная команда:
        run_command = [ Config.strace_command ] + strace_args + [ Config.rpmbuild_command ] + rpmbuild_args_strace


        # --------------
        # Создание базового выходного каталога, если его еще нет
        self.output_dir.mkdir(mode=0o755, parents=True, exist_ok=True)

        # --------------
        # Создание выходного каталога для strace
        Path(self.output_dir / 'strace').mkdir(mode=0o755, parents=True, exist_ok=True)


        # --------------

        # Запись оригинальных аргументов rpmbuild в выходной файл:
        with (self.output_dir / 'rpmbuild.cmd').open('w') as f:
            print(rpmbuild_cmd_orig, file=f, end='')

        # Запись команды в выходной файл:
        with (self.output_dir / 'cmd').open('w') as f:
            print(run_command, file=f, end='')

        # Запись текущего рабочего каталога в котором запущен pvs-rpmbuild в выходной файл:
        with (self.output_dir / 'cwd').open('w') as f:
            print(cwd, file=f, end='')


        # --------------
        # Запуск команды

        returncode = self.__exec_rpmbuild(run_command)

        # --------------
        # Запись кода возврата rpmbuild


        # Запись кода возврата rpmbuild в выходной файл:
        with (self.output_dir / 'rpmbuild.returncode').open('w') as f:
            print(returncode, file=f, end='')

        return (cwd, returncode)


    def __exec_rpmbuild(self, run_command):
        # --------------
        # Запуск команды

        # Сброс буферов ДО
        sys.stdout.flush()
        sys.stderr.flush()

        # Необходимо использовать Popen для возможности привязки sys.stdin, sys.stdout, sys.stderr
        proc = subprocess.Popen(run_command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr);
        proc.wait()

        # Сброс буферов ПОСЛЕ
        sys.stdout.flush()
        sys.stderr.flush()

        return proc.returncode


    # --------------------------------------------------------------
    # Обработка результатов трассировки rpmbuild
    #

    def __print_ignored(self, pid, command, msg):
        self.__print("IGNORED(" + msg + ")[" + str(pid) + "]:", command)

    def __print_preprocessed(self, pid, command, msg):
        self.__print("PREPROCESSED(" + msg + ")[" + str(pid) + "]:", command)


    def __atomic_file_copy(self, copy_src, copy_dst):
        copy_dst_pp = copy_dst.with_suffix(copy_dst.suffix + '.' + str(os.getpid()))
        shutil.copy2(copy_src, copy_dst_pp, follow_symlinks=True)
        copy_dst_pp.rename(copy_dst)


    def __compiler_calls_prefilter(self, compiler_calls : list[CompilerCall]):
        counters = Counter()

        for cc in compiler_calls:
            counters[cc.command.as_tuple()] += 1

        for cc in compiler_calls:
            # Если код завершения не равен 0, то вызов был неудачный
            if cc.exitcode != 0:
                self.__print_ignored(cc.pid, cc.command, "nonzero exit code: {}".format(cc.exitcode))
                continue

            # Если компилятор не открывал файлы - это что-то из области проверки версии.
            if len(cc.open_files) == 0:
                self.__print_ignored(cc.pid, cc.command, "no open files")
                continue

            # Дубликаты - это скорее всего проверки configure, cmake, и т.д.
            # Если компилятор вызывается с одними и теми же параметрами - удалять целиком.
            if (cnt := counters[cc.command.as_tuple()]) > 1:
                self.__print_ignored(cc.pid, cc.command, "multipe calls " + str(cnt))
                continue

            yield cc


    def do_preprocess_compiler_call(self, cc):
        # Если текущего каталога для запуска компилятора нет - игнорируем
        # Остатки cmake или configure
        if not cc.command.cwd.exists():
            self.__print_ignored(cc.pid, cc.command, "cwd not exists")
            return []


        # В параметрах компилятора должен быть хоть один исходник - файл с расширением из таблицы Language.source_exts.
        sources_in_args = self.__compiler_matcher.get_sources_from_args(cc.command)
        if len(sources_in_args) == 0:
            self.__print_ignored(cc.pid, cc.command, "not found sources with allowed exts")
            return []


        # Игнорирование внутрянки CMake
        #   CMakeFiles/3.27.6/CompilerIdCXX/{CMakeCXXCompilerId.cpp,a.out,tmp}
        for source in sources_in_args:
            if os.path.basename(source) in ['CMakeCCompilerId.c', 'CMakeCXXCompilerId.cpp']:
                self.__print_ignored(cc.pid, cc.command, "CMake internal source")
                return []


        # Копирование исходных файлов.
        for of in cc.open_files:
            copy_src = of[0] / of[1][0]
            copy_dst = self.output_dir / 'root' / Path(*copy_src.parts[1:])

            # Файл должен существовать.
            if not copy_src.exists():
                continue

            # Не копируем, если не прошло через фильтр
            if not self.__open_files_filter.allow(copy_src):
                continue

            # Копируем только те файлы, которые открывались для чтения
            # (объектные файлы, результаты, выходные бинарики открываются для записи).
            if 'O_RDONLY' not in of[1][1].split("|"):
                continue

            #print("\t\tCopy:", copy_src, " -> ", copy_dst, flush=True)
            try:
                if copy_src.exists():
                    copy_dst.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    self.__atomic_file_copy(copy_src, copy_dst)
            except Exception:
                pass


        ret = list()
        for idx in range(len(sources_in_args)):
            item = self.__do_preprocess_compiler_call_processing_source(cc, idx, sources_in_args)
            if item is not None:
                ret.append(item)
        return ret


    def __do_preprocess_compiler_call_processing_source(self, cc, source_idx, sources_in_args):
        # Генерация хэша для имени препроцессированного файла
        h = hashlib.sha256()
        h.update(str(cc.command.cwd).encode())
        h.update(b'\x00')
        h.update(cc.command.compiler.id.encode())
        h.update(b'\x00')
        if cc.command.compiler.like:
            h.update(cc.command.compiler.like.encode())
        h.update(b'\x00')
        h.update(str(cc.command.executable).encode())
        h.update(b'\x00')
        for a in cc.command.args:
            h.update(a.encode())
            h.update(b'\x00')
        h.update(b'\x00\x00')
        h.update(sources_in_args[source_idx].encode())
        h.update(b'\x00\x00')

        cc_hash = h.hexdigest()

        preprocessed_file_name  = Path(cc_hash + '.i')
        preprocessed_result_dir = Path('preprocessed') / Path(cc_hash[0:2]) / Path(cc_hash[2:4])

        preprocessed_result_file = preprocessed_result_dir / preprocessed_file_name

        # Генерация команды для препроцессирования файла
        try:
            # Если уже -E есть, то исключение и игнорить команду
            preprocessed_command = self.__compiler_matcher.make_preprocessor_command(cc.command, (self.output_dir / preprocessed_result_file), source_idx, sources_in_args)
        except Exception as e:
            self.__print_ignored(cc.pid, cc.command, "cat't make proprocessor command: {}".format(e))
            return None

        preprocessed_status_msg = 'ok'
        #
        # Генерация препроцессированных файлов и копирование исходных файлов.
        #

        # Создание каталога для препроцессированного файла.
        # print("\tmkdir:", self.output_dir / preprocessed_result_dir)
        try:
            (self.output_dir / preprocessed_result_dir).mkdir(mode=0o755, parents=True, exist_ok=True)
        except Exception:
            pass

        # Генерация препроцессированных файлов
        # Необходимо использовать Popen для возможности привязки sys.stdin, sys.stdout, sys.stderr
        proc = subprocess.Popen(
            preprocessed_command.args, executable=preprocessed_command.executable,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
            cwd=preprocessed_command.cwd
        )
        for line in proc.stdout:
            self.__print(line.rstrip())
        proc.wait()

        if proc.returncode != 0:
            preprocessed_status_msg = "preprocessor nonzero exit code: {}".format(proc.returncode)

        ret_item = None
        # Если вызов препроцессора сломался, значит что-то не то и не надо добавлять
        # такой вызов компилятора в список для проверки анализатором.
        # Скорее всего исходные файлы были удалены.
        if preprocessed_status_msg == 'ok':
            ret_item = ResultItem(
                preprocessed_result_file,
                sources_in_args[source_idx],
                self.__compiler_matcher.get_source_metadata(cc.command, sources_in_args[source_idx]),
                copy.deepcopy(cc.command)
            )

        self.__print_preprocessed(cc.pid, cc.command, preprocessed_status_msg)
        return ret_item


    def __do_preprocessing_compiler_calls(self, compiler_calls_generator) -> list[ResultItem]:
        if self.__parallel > 1:
            with multiprocessing.Pool(processes=self.__parallel) as pool:
                all_results = pool.map(self.do_preprocess_compiler_call, compiler_calls_generator)
        else:
            all_results = (self.do_preprocess_compiler_call(cc) for cc in compiler_calls_generator)

        results : list[ResultItem] = list()
        for rr in all_results:
            if rr is None:
                continue
            results.extend(rr)

        return results


    def __do_preprocessing(self, rpmbuild_cwd):
        # --------------

        self.__print("PARSE-STRACE-START--------------")

        compiler_extractor = CompilerExtractor(
            StraceData(
                SysCallFilter(),
                rpmbuild_cwd,
                Path(self.output_dir / 'strace').glob('trace-rpmbuild.*'),
                self.__parallel
            ),
            self.__compiler_matcher
        )

        self.__print("PARSE-STRACE-END----------------")
        self.timer.cut('parse-strace')

        # --------------

        self.__print("PREPROCESSING-START-------------")

        compiler_calls = compiler_extractor.compiler_calls()
        del compiler_extractor

        results : list[ResultItem] = self.__do_preprocessing_compiler_calls( self.__compiler_calls_prefilter(compiler_calls) )

        self.__print("PREPROCESSING-END---------------")
        self.timer.cut('preprocessing')

        # --------------
        # Конвертация в json и запись

        self.__print("WRITE-RESULT-START--------------")

        with (self.output_dir / 'result.json').open('w') as f:
            json.dump(results, f, cls=ResultEncoder, indent=4)

        self.__print("WRITE-RESULT-END----------------")
        self.timer.cut('write-result')

        # --------------



if __name__ == '__main__':
    app = RpmbuildTracer()
    app.main()
