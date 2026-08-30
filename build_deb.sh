#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
package_name=fetch-version-manager
package_version=2.4.5
stage="$project_dir/build/${package_name}_${package_version}_all"
output_dir="$project_dir/dist"

mkdir -p \
    "$stage/DEBIAN" \
    "$stage/usr/bin" \
    "$stage/usr/lib/$package_name" \
    "$stage/usr/share/applications" \
    "$stage/usr/share/icons/hicolor/scalable/apps" \
    "$stage/usr/share/doc/$package_name" \
    "$output_dir"

install -m 0644 "$project_dir/debian/control" "$stage/DEBIAN/control"
install -m 0755 "$project_dir/debian/postinst" "$stage/DEBIAN/postinst"
install -m 0755 "$project_dir/debian/postrm" "$stage/DEBIAN/postrm"
install -m 0755 "$project_dir/src/fetch_version_manager.py" \
    "$stage/usr/lib/$package_name/fetch_version_manager.py"
install -m 0644 "$project_dir/data/fetch-version-manager.desktop" \
    "$stage/usr/share/applications/fetch-version-manager.desktop"
install -m 0644 "$project_dir/data/fetch-version-manager.svg" \
    "$stage/usr/share/icons/hicolor/scalable/apps/fetch-version-manager.svg"
install -m 0644 "$project_dir/README.md" "$stage/usr/share/doc/$package_name/README.md"
install -m 0644 "$project_dir/INSTALL.md" "$stage/usr/share/doc/$package_name/INSTALL.md"
install -m 0755 "$project_dir/data/fetch-version-manager" \
    "$stage/usr/bin/fetch-version-manager"

dpkg-deb --root-owner-group --build "$stage" \
    "$output_dir/${package_name}_${package_version}_all.deb"
