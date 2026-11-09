#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

python3 - "$project_dir/build_config.json" <<'PY'
import json
import re
import sys
from pathlib import Path

config_file = Path(sys.argv[1])
try:
    config = json.loads(config_file.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("配置必须为 JSON 对象")
    repository = config.get("update_repository")
    if (not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository)
            or repository.rsplit("/", 1)[-1] in (".", "..")):
        raise ValueError("update_repository 必须为有效的 GitHub owner/repo")
except (OSError, UnicodeError, ValueError) as error:
    raise SystemExit(f"构建配置无效（{config_file}）：{error}")
PY

package_name=code-version-manager
package_version=$(tr -d '\r\n' < "$project_dir/VERSION")
stage="$project_dir/build/${package_name}_${package_version}_all"
output_dir="$project_dir/dist"

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
sed -i "s/@VERSION@/$package_version/" "$stage/DEBIAN/control"
install -m 0755 "$project_dir/debian/postinst" "$stage/DEBIAN/postinst"
install -m 0755 "$project_dir/debian/postrm" "$stage/DEBIAN/postrm"
install -m 0755 "$project_dir/src/code_version_manager.py" \
    "$stage/usr/lib/$package_name/code_version_manager.py"
install -m 0644 "$project_dir/build_config.json" \
    "$stage/usr/lib/$package_name/build_config.json"
install -m 0644 "$project_dir/data/workspace.gitignore" \
    "$stage/usr/lib/$package_name/workspace.gitignore"
install -m 0644 "$project_dir/VERSION" "$stage/usr/lib/$package_name/VERSION"
install -m 0644 "$project_dir/data/bcompare/BCSessions.xml" \
    "$stage/usr/lib/$package_name/bcompare/BCSessions.xml"
install -m 0644 "$project_dir/data/code-version-manager.desktop" \
    "$stage/usr/share/applications/code-version-manager.desktop"
install -m 0644 "$project_dir/data/code-version-manager.svg" \
    "$stage/usr/share/icons/hicolor/scalable/apps/code-version-manager.svg"
install -m 0644 "$project_dir/README.md" "$stage/usr/share/doc/$package_name/README.md"
install -m 0644 "$project_dir/INSTALL.md" "$stage/usr/share/doc/$package_name/INSTALL.md"
install -m 0644 "$project_dir/HELP.md" "$stage/usr/share/doc/$package_name/HELP.md"
install -m 0755 "$project_dir/data/code-version-manager" \
    "$stage/usr/bin/code-version-manager"

dpkg-deb --root-owner-group --build "$stage" \
    "$output_dir/${package_name}_${package_version}_all.deb"

(
    cd -- "$output_dir"
    sha256sum "${package_name}_${package_version}_all.deb" > \
        "${package_name}_${package_version}_all.deb.sha256"
)
