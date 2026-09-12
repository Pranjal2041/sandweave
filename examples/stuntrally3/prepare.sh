#!/bin/sh
# Download the official portable game and VirtualGL; no host installation.
set -eu
directory=${1:?Usage: prepare.sh DOWNLOAD_DIRECTORY}
mkdir -p "$directory"
cd "$directory"
if [ ! -f StuntRally-3.3-Linux.txz ]; then
    curl -fL --retry 3 -o StuntRally-3.3-Linux.txz \
        https://downloads.sourceforge.net/project/stuntrally/3.3/StuntRally-3.3-Linux.txz
fi
if [ ! -f virtualgl_3.1.5_amd64.deb ]; then
    curl -fL --retry 3 -o virtualgl_3.1.5_amd64.deb \
        https://github.com/VirtualGL/virtualgl/releases/download/3.1.5/virtualgl_3.1.5_amd64.deb
fi
sha256sum -c <<'CHECKSUMS'
6a509094158c21d40ba9cfb7487c0d10763699dbf117a435daad9309cb326fbb  StuntRally-3.3-Linux.txz
df3f7788ce41b182a47c0d298e5cd6d2d63579522cb41825970b7726e825485e  virtualgl_3.1.5_amd64.deb
CHECKSUMS
if [ -e StuntRally-3.3-Linux ]; then
    echo 'StuntRally-3.3-Linux already exists; keeping the existing extraction.'
else
    # The upstream .txz is gzip-compressed. Let tar detect the format.
    tar -xf StuntRally-3.3-Linux.txz
    # The guest desktop user must be able to read this shared game directory.
    chmod -R a+rX StuntRally-3.3-Linux
fi
