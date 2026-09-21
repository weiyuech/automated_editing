"""Durable music labels, independent of transient media IDs rebuilt by scanning."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from automated_video_editing_backend.core.store import read_json, write_json

DEFAULT_LABELS = ["轻快", "舒缓"]


def label_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 20 or any(ord(char) < 32 for char in name):
        raise ValueError("标签名称应为 1 至 20 个字符")
    if name in {"全部", "未分类"}:
        raise ValueError("这是系统分类，请使用其他标签名称")
    return name


class MusicLabels:
    def __init__(self, path: Path):
        self.path = path
        self.problem = ""
        self.data = {"labels": list(DEFAULT_LABELS), "files": {}}
        raw, problem = read_json(path)
        if problem or (raw is None and list(path.parent.glob(path.name + ".corrupt-*"))):
            self.problem = problem or "音乐标签文件曾损坏，请先恢复该文件"
            return
        if raw is None:
            return
        try:
            if (
                not isinstance(raw, dict)
                or not isinstance(raw.get("labels"), list)
                or not isinstance(raw.get("files"), dict)
            ):
                raise ValueError("音乐标签记录格式无效")
            labels = list(
                dict.fromkeys(DEFAULT_LABELS + [label_name(name) for name in raw["labels"]])
            )
            for key, tags in raw["files"].items():
                if (
                    not isinstance(key, str)
                    or not isinstance(tags, list)
                    or any(tag not in labels for tag in tags)
                ):
                    raise ValueError("音乐文件的标签记录无效")
            self.data = {"labels": labels, "files": raw["files"]}
        except (ValueError, TypeError, AttributeError) as exc:
            self.problem = f"音乐标签无法读取：{exc}"

    def require_ready(self):
        if self.problem:
            raise ValueError(self.problem)

    def commit(self, data):
        self.require_ready()
        if not write_json(self.path, data):
            raise ValueError("音乐标签保存失败，请检查磁盘空间和文件权限")
        self.data = data

    def labels(self) -> list[str]:
        self.require_ready()
        return list(self.data["labels"])

    def create(self, value: str) -> list[str]:
        name = label_name(value)
        labels = self.labels()
        if name not in labels:
            if len(labels) >= 200:
                raise ValueError("最多支持 200 个音乐标签")
            updated = deepcopy(self.data)
            updated["labels"].append(name)
            self.commit(updated)
        return self.labels()

    def for_path(self, path: str) -> list[str]:
        self.require_ready()
        return list(self.data["files"].get(path, []))

    def assign(self, path: str, labels: list[str]) -> list[str]:
        known = self.labels()
        tags = list(dict.fromkeys(labels))
        if len(tags) > 20 or any(tag not in known for tag in tags):
            raise ValueError("请选择已存在的音乐标签，每首最多 20 个")
        updated = deepcopy(self.data)
        if tags:
            updated["files"][path] = tags
        else:
            updated["files"].pop(path, None)
        self.commit(updated)
        return tags

    def move(self, old: str, new: str):
        self.require_ready()
        if old == new or old not in self.data["files"]:
            return
        updated = deepcopy(self.data)
        updated["files"][new] = updated["files"].pop(old)
        self.commit(updated)
