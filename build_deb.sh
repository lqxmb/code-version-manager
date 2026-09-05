#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
package_name=code-version-manager
package_version=$(tr -d '\r\n' < "$project_dir/VERSION")
stage="$project_dir/build/${package_name}_${package_version}_all"
output_dir="$project_dir/dist"
control_version=$(awk '/^Version:/ { print $2; exit }' "$project_dir/debian/control")

if [ "$control_version" != "$package_version" ]; then
    echo "VERSION and debian/control do not match." >&2
    exit 1
fi

mkdir -p \
    "$stage/DEBIAN" \
    "$stage/usr/bin" \
    "$stage/usr/lib/$package_name" \
    "$stage/usr/lib/$package_name/bcompare" \
    "$stage/usr/share/applications" \
    "$stage/usr/share/icons/hicolor/scalable/apps" \
    "$stage/usr/share/doc/$package_name" \
    "$output_dir"

install -m 0644 "$project_dir/debian/control" "$stage/DEBIAN/control"
install -m 0755 "$project_dir/debian/postinst" "$stage/DEBIAN/postinst"
install -m 0755 "$project_dir/debian/postrm" "$stage/DEBIAN/postrm"
install -m 0755 "$project_dir/src/code_version_manager.py" \
    "$stage/usr/lib/$package_name/code_version_manager.py"
install -m 0644 "$project_dir/VERSION" "$stage/usr/lib/$package_name/VERSION"
install -m 0644 "$project_dir/data/bcompare/BCSessions.xml" \
    "$stage/usr/lib/$package_name/bcompare/BCSessions.xml"
install -m 0644 "$project_dir/data/code-version-manager.desktop" \
    "$stage/usr/share/applications/code-version-manager.desktop"
install -m 0644 "$project_dir/data/code-version-manager.svg" \
    "$stage/usr/share/icons/hicolor/scalable/apps/code-version-manager.svg"
install -m 0644 "$project_dir/README.md" "$stage/usr/share/doc/$package_name/README.md"
install -m 0644 "$project_dir/INSTALL.md" "$stage/usr/share/doc/$package_name/INSTALL.md"
install -m 0755 "$project_dir/data/code-version-manager" \
    "$stage/usr/bin/code-version-manager"

dpkg-deb --root-owner-group --build "$stage" \
    "$output_dir/${package_name}_${package_version}_all.deb"
