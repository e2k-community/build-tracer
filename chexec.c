/**
 * Copyright (c) 2025, LLC NIC CT
 * Copyright (c) 2025, Vladislav Shchapov <vladislav@shchapov.ru>
 *
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of the GNU General Public License
 * as published by the Free Software Foundation; either version 2
 * of the License, or any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, see <https://www.gnu.org/licenses/>.
 */

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main (int argc, char *argv[])
{
    int r;

    if (argc < 3)
    {
        fputs("chexec NEWWD COMMAND [ARG]...\n", stderr);
        return 1;
    }

    r = chdir(argv[1]);
    if (r < 0)
    {
        fprintf(stderr, "cd: %s: %s\n", argv[1], strerror(errno));
        return 2;
    }

    r = execvp(argv[2], argv + 2);
    if (r < 0)
    {
        fprintf(stderr, "exec: %s: %s\n", argv[2], strerror(errno));
        return 3;
    }

    return 0;
}
