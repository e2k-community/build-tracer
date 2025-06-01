# Build Tracer

**Build Tracer** - это набор приложений для извлечения исходных кодов, команд компиляции и препроцессированных файлов из процесса сборки и для их дальнейшего анализа.


## Трассировка процесса сборки rpm-пакетов

Для трассировки процесса сборки rpm-пакетов используется утилита `build-tracer-rpmbuild.py`, которая является прозрачной заменой для `rpmbuild`.

Для использования `build-tracer-rpmbuild.py` в первую очередь необходимо склонируйте репозиторий или распакуйте архив в какой-нибудь каталог.
В дальнейшем будем предполагать, что архив распакован в каталог `~/build-tracer`.


Рассмотрим два варианта генерации трассировки.

### Сборка rpm-пакета при помощи rpmbuild

В этом случае необходимо заменить `rpmbuild` на `~/build-tracer/build-tracer-rpmbuild.py` и при помощи переменной окружения `RPM_BUILD_NCPUS` ограничить число параллельных процессов при сборке.

Например, команда сборки пакета fmt может выглядеть следующим образом:

`RPM_BUILD_NCPUS=16 ~/build-tracer/build-tracer-rpmbuild.py --noclean -bb --define "_topdir $HOME/pkg/fmt/rpmbuild/" ~/pkg/fmt/rpmbuild/SOURCES/fmt.spec 2>&1 | tee ~/pkg/fmt/res-$(date +%F-%H-%M).log`

По умолчанию каталог с результатами будет создан в текущем каталоге с именем `build_trace-<pid>`, а внутри будет каталог с именем пакета, полученным путем запуска команды `rpmspec --queryformat=%{nvr} -q --srpm ~/pkg/fmt/rpmbuild/SOURCES/fmt.spec`.

Базовую часть пути можно переопределить с помощью переменной окружения `BUILD_TRACER_OUTPUT_DIR`.


### Сборка rpm-пакета при помощи mock из srpm-пакета

**Установка плагина `build_tracer` в mock**: Скопируйте файл `~/build-tracer/mockbuild/plugins/build_tracer.py` в каталог:
- в операционных системах, построенных на базе RHEL 9:  `/usr/lib/python3.9/site-packages/mockbuild/plugins`;
- в операционных системах, построенных на базе RHEL 10: `/usr/lib/python3.12/site-packages/mockbuild/plugins`.


После установки плагина `build_tracer` необходимо очистить кэш mock и конфигурации mock путем удаления подкаталогов в каталогах `/var/cache/mock` и `/var/lib/mock`.


Для сборки в mock необходимо написать собственный файл конфигурации. Например, `collabos-main-e2kv5-pvs.cfg`, который будет унаследован от стандартной конфигурации `collabos-main-e2kv5`:

```
include('/etc/mock/collabos-main-e2kv5.cfg')
config_opts['root'] += '-pvs'
config_opts['rpmbuild_timeout'] = 0

# Если проект содержит большое количество исходных файлов и вызовов компилятора, то, для сокращения потребления памяти
# на этапе подготовки препроцессированных файлов, раскомментируйте и установите параметр BUILD_TRACER_PARALLEL
#config_opts['environment']['BUILD_TRACER_PARALLEL']    = '1'

# Ограничение на количество параллельных процессов сборки
config_opts['environment']['RPM_BUILD_NCPUS'] = '16'

# Подключение плагина трассирующей сборки
config_opts['plugin_conf']['build_tracer_enable'] = True
config_opts['plugin_conf']['build_tracer_opts'] = {
    # Имя подкаталога и архива с результатами трассировки
    # "dir_name": "build_trace",

    # Путь к strace в контейнере
    # "strace_command": "/usr/bin/strace",

    # Путь к трассирующему rpmbuild на хосте
    "host_trace_rpmbuild_command": os.path.expanduser('~/build-tracer/build-tracer-rpmbuild.py'),
}
```


После этого необходимо запустить mock с использованием данной конфигурации:

```
time mock -r ~/pkg/fmt/collabos-main-e2kv5-pvs.cfg  --resultdir=~/pkg/fmt/result --rebuild ~/pkg/fmt/rpmbuild/SRPMS/fmt-8.1.1-5.cos1.src.rpm --verbose 2>&1 | tee res-$(date +%F-%H-%M).log
```

Если при трассировке сборки тесты начинают работать нестабильно, то необходимо добавить параметр `--nocheck` для отключения их запуска.

Если необходим анализ содержимого контейнера после сборки, то добавьте параметр `--no-cleanup-after`. В этом случае необработанные данные трассировки будут в каталоге `/var/lib/mock/collabos-main-e2kv5-pvs/root/builddir/build_trace` в подкаталоге с именем пакета. Для fmt - `fmt-8.1.1-5.cos1`.


Параметр `--resultdir=~/pkg/fmt/result` указывает, что mock скопирует результаты сборки в том числе результаты трассировки в указанный каталог.



## Анализ кода на x86_64 сервере с CentOS >= 9 и podman > 4.5.1

### Сборка podman образа контейнера

```
~/build-tracer/build-tracer-analyzer-pvs.sh build
```

### Запуск анализа

```

~/build-tracer/build-tracer-analyzer-pvs.sh analyze         \
        /home/phprus/data/fmt/build_trace                   \
        /home/phprus/data/fmt/build_trace-pvs-result        \
        /home/phprus/pvs/pvs-studio-7.36.91321.455-x86_64   \
    [--parallel=N --]                                       \
    [pvs-studio args...]
```


- `/home/phprus/data/fmt/build_trace` - путь к каталогу с исходными данными, который был сгенерирован запуском сборки пакета с помощью `build-tracer-rpmbuild.py`.
- `/home/phprus/data/fmt/build_trace-pvs-result` - путь к каталогу с результатами анализа (**ВНИМАНИЕ:** Не может быть вложен в каталог с исходными данными).
- `--parallel=N --` - возможность задать количество процессов для параллельного анализа.
- `pvs-studio args` - аргументы для `pvs-studio`, в том числе имя и ключь лицензии.


