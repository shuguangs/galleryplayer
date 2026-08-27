"""设置界面：图形化编辑 config.yaml（Tkinter，零额外依赖）。

用法：python settings_gui.py
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import yaml

CFG = Path(__file__).resolve().parent / "config.yaml"
EXAMPLE = Path(__file__).resolve().parent / "config.example.yaml"

MODELS = ["tiny", "base", "small", "medium", "large-v3"]
DEVICES = ["cuda", "cpu"]
LANGS = ["en", "ja", "ko", "fr", "de", "es", "it", "ru", "auto"]
OLLAMA_MODELS = ["qwen2.5:7b", "qwen2.5:3b", "qwen2.5:1.5b", "llama3.1:8b"]


class SettingsApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("Live Subtitle 设置")
        root.geometry("560x400")
        self.cfg = self._load()

        frm = ttk.Frame(root, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="识别（whisper）", font=("", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(frm, text="模型").grid(row=1, column=0, sticky="e", padx=6)
        self.asr_model = ttk.Combobox(frm, values=MODELS, width=14)
        self.asr_model.set(self.cfg["asr"]["model"])
        self.asr_model.grid(row=1, column=1, sticky="w")
        ttk.Label(frm, text="设备").grid(row=1, column=2, sticky="e", padx=6)
        self.asr_device = ttk.Combobox(frm, values=DEVICES, width=6)
        self.asr_device.set(self.cfg["asr"]["device"])
        self.asr_device.grid(row=1, column=3, sticky="w")
        ttk.Label(frm, text="原声语言").grid(row=2, column=0, sticky="e", padx=6)
        self.asr_lang = ttk.Combobox(frm, values=LANGS, width=10)
        self.asr_lang.set(self.cfg["asr"]["language"])
        self.asr_lang.grid(row=2, column=1, sticky="w")

        ttk.Label(frm, text="翻译（Ollama 本地模型）", font=("", 10, "bold")).grid(row=4, column=0, sticky="w", pady=(14, 6))
        self.tr_enabled = tk.BooleanVar(value=bool(self.cfg["translate"]["enabled"]))
        ttk.Checkbutton(frm, text="启用翻译", variable=self.tr_enabled).grid(row=5, column=0, sticky="w")
        ttk.Label(frm, text="模型").grid(row=5, column=2, sticky="e", padx=6)
        self.tr_model = ttk.Combobox(frm, values=OLLAMA_MODELS, width=14)
        self.tr_model.set(self.cfg["translate"]["model"])
        self.tr_model.grid(row=5, column=3, sticky="w")
        ttk.Label(frm, text="目标语言").grid(row=6, column=0, sticky="e", padx=6)
        self.tr_lang = ttk.Entry(frm, width=10)
        self.tr_lang.insert(0, self.cfg["translate"]["target_lang"])
        self.tr_lang.grid(row=6, column=1, sticky="w")

        ttk.Label(frm, text="输出", font=("", 10, "bold")).grid(row=8, column=0, sticky="w", pady=(14, 6))
        self.out_orig = tk.BooleanVar(value=bool(self.cfg["output"]["show_original"]))
        ttk.Checkbutton(frm, text="同时显示原语", variable=self.out_orig).grid(row=9, column=0, sticky="w")
        self.out_srt = tk.BooleanVar(value=bool(self.cfg["output"]["srt"]))
        ttk.Checkbutton(frm, text="导出 .srt 字幕文件", variable=self.out_srt).grid(row=9, column=1, sticky="w")

        bar = ttk.Frame(frm)
        bar.grid(row=11, column=0, columnspan=4, pady=(18, 0), sticky="ew")
        ttk.Button(bar, text="测试 Ollama", command=self._ping).pack(side="left")
        ttk.Button(bar, text="保存", command=self._save).pack(side="right")
        ttk.Button(bar, text="保存并关闭", command=lambda: (self._save(), root.destroy())).pack(side="right", padx=6)
        self.status = ttk.Label(frm, text="", foreground="#2a6")
        self.status.grid(row=12, column=0, columnspan=4, sticky="w", pady=(8, 0))

    def _load(self) -> dict:
        if CFG.is_file():
            return yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
        if EXAMPLE.is_file():
            return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")) or {}
        return {}

    def _ping(self) -> None:
        import urllib.request

        try:
            urllib.request.urlopen(f"{self.cfg['translate']['endpoint']}/api/tags", timeout=5)
            self.status.config(text="Ollama 在线 ✓", foreground="#2a6")
        except Exception:  # noqa: BLE001
            self.status.config(text="Ollama 离线 ✗（先启动 Ollama）", foreground="#c33")

    def _save(self) -> None:
        cfg = self.cfg
        cfg["asr"]["model"] = self.asr_model.get()
        cfg["asr"]["device"] = self.asr_device.get()
        cfg["asr"]["language"] = self.asr_lang.get()
        cfg["asr"]["compute"] = "float16" if cfg["asr"]["device"] == "cuda" else "int8"
        cfg["translate"]["enabled"] = self.tr_enabled.get()
        cfg["translate"]["model"] = self.tr_model.get()
        cfg["translate"]["target_lang"] = self.tr_lang.get().strip() or "zh"
        cfg["output"]["show_original"] = self.out_orig.get()
        cfg["output"]["srt"] = self.out_srt.get()
        CFG.write_text(yaml.safe_dump(cfg, allow_unicode=True, default_flow_style=False),
                       encoding="utf-8")
        self.status.config(text="已保存 config.yaml ✓", foreground="#2a6")
        messagebox.showinfo("完成", "配置已保存到 config.yaml")


def main() -> None:
    root = tk.Tk()
    SettingsApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()