# Homebrew dependencies for Moonglade (easy-setup). `./bootstrap` checks them with
#   brew bundle check --no-upgrade --file=Brewfile
# and, with your OK (or --yes), installs what's missing with `brew bundle install --no-upgrade`: it never upgrades
# or removes anything you already have. Versions on the dev Mac (2026-10-05): cmake 3.31, mysql 9.2, boost 1.92,
# openssl@3 3.6, readline 8, ccache 4.14, python 3.14.
brew "cmake"        # server build (harness/hslib/build.py cmake_args)
brew "ccache"       # compiler cache for the server build
brew "boost"        # server build dependency
brew "openssl@3"    # server build dependency
brew "readline"     # server build dependency
brew "mysql"        # client library for the build, and the repo's own isolated mysqld (ADR-0002)
brew "python"       # harness/hs, ./play and ./bootstrap need Python 3.10 or newer
