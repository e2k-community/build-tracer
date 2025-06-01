#!/bin/sh

APP_NAME=$(basename "$0")


print_usage()
{
    cat <<USAGE
Usage

Rebuild podman image:
    $APP_NAME build

Run pvs-analyzer:
    $APP_NAME analyze build_trace-dir result-dir pvs-dir [args...]

    Analyze souces from build_trace directory and store results into result directory.

USAGE
}


if [ "x$1" = "xbuild" ]
then
    if [ $# -ne 1 ]
    then
        echo "Error: Invalid number of arguments. Expected 1, passed $#\n"
        echo "\n"
        print_usage
        exit 2
    fi

    podman build --squash -t build-tracer-analyzer-pvs -f Dockerfile.build-tracer-analyzer-pvs

elif [ "x$1" = "xanalyze" ]
then
    if [ $# -lt 4 ]
    then
        echo "Error: Invalid number of arguments. Expected >= 4, passed $#\n"
        echo "\n"
        print_usage
        exit 2
    fi

    if [ ! -d "$2" ]
    then
        echo "Error: Source directory not exists or is not a directory\n"
        echo "\n"
        print_usage
        exit 3
    fi

    if [ ! -d "$3" ]
    then
        echo "Error: Destination directory not exists or is not a directory\n"
        echo "\n"
        print_usage
        exit 4
    fi

    if [ ! -d "$4" ]
    then
        echo "Error: PVS directory not exists or is not a directory\n"
        echo "\n"
        print_usage
        exit 4
    fi

    build_trace_dir="$2"
    result_dir="$3"
    pvs_dir="$4"
    shift # action
    shift # build_trace-dir
    shift # result-dir
    shift # pvs-dir



    for pkg in $(find "$build_trace_dir" -mindepth 1 -maxdepth 1 -type d | xargs -n1 basename);
    do
        echo "Analyze: $pkg"
        echo ""

        mkdir -p "$result_dir/$pkg"

        podman run --rm -it --security-opt label=disable --cap-add=SYS_CHROOT           \
                -v "$build_trace_dir/$pkg":/data:O                                      \
                -v "$build_trace_dir/$pkg/preprocessed":/data/root/pvs/preprocessed:O   \
                -v "$result_dir/$pkg":/data/root/pvs/result:rw                          \
                -v "$pvs_dir/bin":/data/root/pvs/bin:O                                  \
            build-tracer-analyzer-pvs "$@"

        find "$result_dir/$pkg/" -name '*.PVS-Studio.log' | xargs cat > "$result_dir/$pkg".PVS-Studio.log

    done

else
    print_usage
    exit 1
fi
