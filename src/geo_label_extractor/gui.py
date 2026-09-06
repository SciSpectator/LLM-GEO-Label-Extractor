"""Qt desktop launcher for the unified publication pipeline."""
from __future__ import annotations

import shlex
import sys
import os
from pathlib import Path

try:
    from PySide6.QtCore import QProcess, Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout,
        QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
        QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
        QListWidget, QListWidgetItem, QStackedWidget,
    )
except ImportError as exc:
    raise SystemExit("GUI requires: pip install -r requirements-gui.txt") from exc

try:                                  # imported as part of the package
    from . import metadata_fields
except ImportError:                   # or run directly from the source tree
    import metadata_fields

LABELS = ("Sex", "Age", "Tissue", "Condition", "Treatment")
STYLE = """
QMainWindow, QWidget { background:#fff; color:#1f2329; font-family:"Segoe UI",Inter,sans-serif; font-size:13px; }
QWidget#sidebar { background:#f7f8fa; border-right:1px solid #e6e8eb; }
QLabel#brand { color:#2563eb; font-size:24px; font-weight:800; }
QLabel#brandsub { color:#4f46e5; font-size:15px; font-weight:600; }
QLabel#pageTitle { font-size:20px; font-weight:700; }
QLabel#muted { color:#7a828d; }
QListWidget#nav { background:transparent; border:none; outline:0; font-size:14px; }
QListWidget#nav::item { padding:12px 14px; border-radius:8px; margin:3px 8px; color:#5b626c; }
QListWidget#nav::item:selected { background:#e8effd; color:#2563eb; font-weight:600; }
QListWidget#nav::item:hover:!selected { background:#eef0f3; }
QGroupBox { background:#fff; border:1px solid #e6e8eb; border-radius:12px;
 margin-top:16px; padding:15px 14px 10px; font-weight:600; }
QGroupBox::title { subcontrol-origin:margin; left:14px; padding:0 6px; color:#6b7280; }
QLineEdit,QComboBox,QSpinBox,QPlainTextEdit { background:#fff; border:1px solid #d6d9de;
 border-radius:8px; padding:7px 10px; selection-background-color:#2563eb; }
QLineEdit:focus,QComboBox:focus,QPlainTextEdit:focus { border-color:#2563eb; }
QPushButton { background:#2563eb; color:#fff; border:none; border-radius:8px; padding:9px 18px; font-weight:600; }
QPushButton:hover { background:#1d54cf; } QPushButton:disabled { background:#e3e5e8; color:#9aa1aa; }
QPushButton#secondary { background:#fff; color:#1f2329; border:1px solid #d6d9de; }
QPushButton#secondary:hover { background:#f3f4f6; }
QCheckBox { spacing:9px; padding:8px; } QCheckBox::indicator { width:18px; height:18px; }
QTabWidget::pane { border:1px solid #e6e8eb; border-radius:10px; top:-1px; }
QTabBar::tab { background:#f7f8fa; padding:9px 18px; margin-right:2px; }
QTabBar::tab:selected { background:#e8effd; color:#2563eb; font-weight:600; }
QPlainTextEdit#log { background:#fbfbfc; color:#3b424b; font-family:"JetBrains Mono",monospace; }
"""


class FileRow(QWidget):
    def __init__(self, title: str, directory: bool = False):
        super().__init__(); self.directory = directory
        layout = QHBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(); self.edit.setPlaceholderText(title)
        button = QPushButton("Browse"); button.setObjectName("secondary")
        button.clicked.connect(self.browse)
        layout.addWidget(self.edit, 1); layout.addWidget(button)

    def browse(self):
        value = (QFileDialog.getExistingDirectory(self, "Choose directory")
                 if self.directory else
                 QFileDialog.getOpenFileName(self, "Choose file")[0])
        if value: self.edit.setText(value)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__(); self.process = QProcess(self)
        self.setWindowTitle("Context-aware LLM GEO Label Extractor"); self.resize(1180, 800)
        root = QWidget(); self.setCentralWidget(root)
        shell = QHBoxLayout(root); shell.setContentsMargins(0, 0, 0, 0); shell.setSpacing(0)
        sidebar = QWidget(); sidebar.setObjectName("sidebar"); sidebar.setFixedWidth(220)
        side = QVBoxLayout(sidebar); side.setContentsMargins(18, 24, 12, 18)
        brand = QLabel("LLM GEO"); brand.setObjectName("brand")
        sub = QLabel("Context-aware Label Extractor"); sub.setObjectName("brandsub"); sub.setWordWrap(True)
        side.addWidget(brand); side.addWidget(sub); side.addSpacing(24)
        self.nav = QListWidget(); self.nav.setObjectName("nav")
        for text in ("1   Select samples", "2   Choose labels", "3   Configure & run", "4   Results"):
            self.nav.addItem(QListWidgetItem(text))
        side.addWidget(self.nav, 1); shell.addWidget(sidebar)
        self.stack = QStackedWidget(); shell.addWidget(self.stack, 1)

        select_page, layout = self._page(
            "Select GEO samples", "Choose exact GSMs, whole GPL platforms, or describe the desired subset in words.")

        files = QGroupBox("Data and controlled-vocabulary resources")
        form = QFormLayout(files)
        # Not necessarily GEOmetadb: any SQLite with a gsm table works, and the
        # column panel reads whatever that table happens to hold.
        self.database = FileRow("Sample metadata SQLite")
        self.vocab = FileRow("vocab.sqlite")
        self.index = FileRow("vocab_index.npz")
        self.cells = FileRow("cellosaurus.sqlite")
        self.output = FileRow("Output directory", directory=True)
        for name, widget in (("Sample metadata", self.database), ("MeSH vocabulary", self.vocab),
                             ("Semantic index", self.index), ("Cellosaurus", self.cells),
                             ("Results", self.output)): form.addRow(name, widget)
        detect_row = QHBoxLayout(); self.detect_status = QLabel(""); self.detect_status.setObjectName("muted")
        detect = QPushButton("Auto-detect databases"); detect.setObjectName("secondary")
        detect.clicked.connect(lambda: self.auto_detect_resources(True))
        detect_row.addWidget(detect); detect_row.addWidget(self.detect_status, 1)
        form.addRow("", detect_row)

        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        self.gsm_text = self._selection_tab(
            "One GSM per line, or paste a CSV/TSV first column.", "Load GSM file")
        self.gpl_text = self._selection_tab(
            "One GPL per line. Every matching GSM is selected.", "Load GPL file")
        spec_page = QWidget(); spec_layout = QVBoxLayout(spec_page)
        spec_layout.addWidget(QLabel(
            "Example: Homo sapiens breast cancer, Tissue and Condition, Phase 1b only\n"
            "The configured LLM converts the request into a GEO search plan."))
        self.spec_text = QPlainTextEdit(); spec_layout.addWidget(self.spec_text)
        self.tabs.addTab(spec_page, "AI Assistant")
        note = QLabel("The AI Assistant interprets your request and searches the local GEOmetadb catalogue.")
        note.setObjectName("muted"); layout.addWidget(note)

        # GEO holds every assay type and the extractor reads only the text a
        # sample record carries, which exists whatever the platform measured.
        # These two narrow the selection; the pipeline itself is unchanged.
        scope = QGroupBox("Sample scope"); scope_form = QFormLayout(scope)
        self.tech = QComboBox(); self.tech.setEditable(True)
        self.tech.addItems(["", "high-throughput sequencing", "in situ oligonucleotide",
                            "spotted DNA/cDNA", "spotted oligonucleotide",
                            "oligonucleotide beads"])
        self.organism = QComboBox(); self.organism.setEditable(True)
        self.organism.addItems(["", "Homo sapiens", "Mus musculus", "Rattus norvegicus"])
        scope_form.addRow("Modality", self.tech)
        scope_form.addRow("Organism", self.organism)
        scope_note = QLabel("Leave either blank to accept every value. RNA-seq and "
                            "single-cell platforms are 'high-throughput sequencing'; "
                            "arrays are the remaining entries.")
        scope_note.setObjectName("muted"); scope_note.setWordWrap(True)
        scope_form.addRow("", scope_note)
        layout.addWidget(scope)

        # Which columns the model reads. A GEO record keeps its useful text in
        # different columns depending on the platform and the submitter, so the
        # choice belongs to the user -- but the default is the published set,
        # because moving it makes a run incomparable with the paper.
        cols = QGroupBox("Metadata columns the model reads")
        cols_layout = QVBoxLayout(cols)
        self.fields = QListWidget(); self.fields.setMaximumHeight(190)
        self._fill_fields(metadata_fields.DEFAULT_COLUMNS)
        cols_layout.addWidget(self.fields)
        row = QHBoxLayout()
        load = QPushButton("Read columns from the selected database")
        load.clicked.connect(self._load_fields)
        reset = QPushButton("Restore published set")
        reset.clicked.connect(
            lambda: self._fill_fields(metadata_fields.DEFAULT_COLUMNS))
        row.addWidget(load); row.addWidget(reset); row.addStretch(1)
        cols_layout.addLayout(row)
        cols_note = QLabel(
            "Ticked columns are placed in front of the model. The five ticked "
            "by default are the ones the published run read; measured over "
            "17,071 samples, other columns held label text absent from these "
            "five for under 0.5% of samples.")
        cols_note.setObjectName("muted"); cols_note.setWordWrap(True)
        cols_layout.addWidget(cols_note)
        layout.addWidget(cols)
        self.stack.addWidget(select_page)

        labels_page, layout = self._page(
            "Choose labels", "Select any combination. Age uses its dedicated reasoning-enabled extraction path.")
        options = QGroupBox("Pipeline configuration"); grid = QGridLayout(options)
        self.labels = {}
        for i, label in enumerate(LABELS):
            box = QCheckBox(label); box.setChecked(True); self.labels[label] = box
            grid.addWidget(box, 0, i)
        self.phase = QComboBox(); self.phase.addItem("Phase 1 — verbatim extraction", "phase1")
        self.phase.addItem("Phase 1 + Phase 1b — add GSE context", "phase1b")
        self.phase.addItem("Full pipeline — normalize, merge, and ship the final corpus", "phase2")
        self.phase.setCurrentIndex(2)
        self.phase_help = QLabel(); self.phase_help.setWordWrap(True)
        self.phase.currentIndexChanged.connect(self.update_phase_help)
        self.backend = QComboBox(); self.backend.addItems(["vllm", "sglang", "openai"])
        self.url = QLineEdit("http://127.0.0.1:8000/v1")
        self.p1_model = QLineEdit("google/gemma-4-12b-it")
        self.age_model = QLineEdit("google/gemma-4-e2b-it")
        self.p2_model = QLineEdit("google/gemma-4-e2b-it")
        self.extract_workers = QSpinBox(); self.extract_workers.setRange(1, 1024); self.extract_workers.setValue(16)
        self.normalize_workers = QSpinBox(); self.normalize_workers.setRange(1, 4096); self.normalize_workers.setValue(64)
        descriptions = {
            "Tissue": "Anatomical source, primary cell type, or named cell line.",
            "Condition": "Disease, phenotype, or biological/control state.",
            "Treatment": "Drug, dose, perturbation, infection, or control intervention.",
            "Sex": "Male, Female, Mixed, or Not Specified.",
            "Age": "Donor age or developmental stage; separate e2B reasoning path.",
        }
        for row, name in enumerate(LABELS, 1):
            grid.addWidget(QLabel(descriptions[name]), row, 0, 1, 5)
        grid.setColumnStretch(4, 1)
        self.update_phase_help()
        layout.addWidget(options)
        layout.addStretch(); self.stack.addWidget(labels_page)

        run_page, layout = self._page(
            "Configure and run", "Run verbatim extraction only, add GSE-context recovery, or execute the full normalized pipeline.")
        layout.addWidget(files)
        config = QGroupBox("Models, backend, and scale"); grid = QGridLayout(config)
        grid.addWidget(QLabel("Stop after"), 0, 0); grid.addWidget(self.phase, 0, 1, 1, 3)
        grid.addWidget(QLabel("Backend"), 1, 0); grid.addWidget(self.backend, 1, 1)
        grid.addWidget(QLabel("Endpoint"), 1, 2); grid.addWidget(self.url, 1, 3)
        grid.addWidget(QLabel("Phase 1 / 1b model"), 2, 0); grid.addWidget(self.p1_model, 2, 1, 1, 3)
        grid.addWidget(QLabel("Age reasoning model"), 3, 0); grid.addWidget(self.age_model, 3, 1, 1, 3)
        grid.addWidget(QLabel("Phase 2 reasoning model"), 4, 0); grid.addWidget(self.p2_model, 4, 1, 1, 3)
        grid.addWidget(QLabel("Extraction workers"), 5, 0); grid.addWidget(self.extract_workers, 5, 1)
        grid.addWidget(QLabel("Normalization workers"), 5, 2); grid.addWidget(self.normalize_workers, 5, 3)
        grid.addWidget(self.phase_help, 6, 0, 1, 4)
        layout.addWidget(config)

        buttons = QHBoxLayout(); self.preview = QPushButton("Preview command")
        self.preview.setObjectName("secondary"); self.start = QPushButton("Start extraction")
        self.stop = QPushButton("Stop"); self.stop.setEnabled(False)
        self.preview.clicked.connect(self.preview_command); self.start.clicked.connect(self.run)
        self.stop.clicked.connect(self.process.kill)
        buttons.addWidget(self.preview); buttons.addStretch(); buttons.addWidget(self.start); buttons.addWidget(self.stop)
        layout.addLayout(buttons)
        layout.addStretch(); self.stack.addWidget(run_page)

        results_page, layout = self._page("Results", "Live pipeline output, checkpoints, warnings, and completion status.")
        self.log = QPlainTextEdit(); self.log.setObjectName("log"); self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        self.stack.addWidget(results_page)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex); self.nav.setCurrentRow(0)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(self.read_error)
        self.process.finished.connect(self.finished)
        self.auto_detect_resources(False)

    @staticmethod
    def _page(title: str, subtitle: str):
        page = QWidget(); layout = QVBoxLayout(page); layout.setContentsMargins(30, 26, 30, 24); layout.setSpacing(12)
        heading = QLabel(title); heading.setObjectName("pageTitle"); layout.addWidget(heading)
        note = QLabel(subtitle); note.setObjectName("muted"); note.setWordWrap(True); layout.addWidget(note)
        return page, layout

    def _selection_tab(self, help_text: str, button_text: str):
        page = QWidget(); layout = QVBoxLayout(page); layout.addWidget(QLabel(help_text))
        editor = QPlainTextEdit(); layout.addWidget(editor)
        button = QPushButton(button_text); button.setObjectName("secondary")
        button.clicked.connect(lambda: self.load_list(editor)); layout.addWidget(button)
        self.tabs.addTab(page, button_text.replace("Load ", "").replace(" file", " list"))
        return editor

    def auto_detect_resources(self, deep: bool = False):
        targets = {
            "geometadb.sqlite": self.database.edit,
            "vocab.sqlite": self.vocab.edit,
            "vocab_index.npz": self.index.edit,
            "cellosaurus.sqlite": self.cells.edit,
        }
        roots = [Path.cwd(), Path.home(), Path(__file__).resolve().parents[2]]
        suffixes = (Path(), Path("data"), Path("data/reference"), Path("reference"))
        found = {}
        for root in roots:
            for suffix in suffixes:
                directory = root / suffix
                if not directory.is_dir(): continue
                available = {path.name.casefold(): path for path in directory.iterdir() if path.is_file()}
                for filename in targets:
                    if filename not in found and filename in available: found[filename] = available[filename]
        if deep and len(found) < len(targets):
            skip = {".git", ".cache", ".local", ".venv", "node_modules", "__pycache__"}
            for base, dirs, files in os.walk(Path.home()):
                depth = len(Path(base).relative_to(Path.home()).parts)
                dirs[:] = [] if depth >= 6 else [name for name in dirs if name not in skip]
                available = {name.casefold(): name for name in files}
                for filename in targets:
                    if filename not in found and filename in available:
                        found[filename] = Path(base) / available[filename]
                if len(found) == len(targets): break
        for filename, path in found.items():
            if not targets[filename].text().strip(): targets[filename].setText(str(path))
        if not self.output.edit.text().strip(): self.output.edit.setText(str(Path.cwd() / "results"))
        missing = [name for name, field in targets.items() if not field.text().strip()]
        suffix = f" · missing: {', '.join(missing)}" if missing else " · ready"
        self.detect_status.setText(f"Detected {len(targets) - len(missing)}/{len(targets)} resources{suffix}")

    def update_phase_help(self):
        descriptions = {
            "phase1": "Gemma 4 12B extracts T/C/T/Sex verbatim; the separate Gemma 4 e2B reasoning path extracts Age.",
            "phase1b": "Phase 1 plus bounded 12B recovery from GSE title, summary, design, and sibling labels.",
            "phase2": "Full connected pipeline: Phase 1 + 1b, then e2B reasoning with MeSH, Cellosaurus, BioLORD/SapBERT, caches, and OOV consolidation, then final assembly — merge every platform into one five-label corpus and relocate duplicate-id labels.",
        }
        self.phase_help.setText(descriptions[self.phase.currentData()])

    def load_list(self, editor):
        path = QFileDialog.getOpenFileName(self, "Load accession list")[0]
        if path: editor.setPlainText(Path(path).read_text(errors="replace"))

    def _write_manifest(self, name: str, text: str, prefix: str) -> str | None:
        values = []
        for line in text.splitlines():
            value = line.strip().split(",")[0].split("\t")[0].upper()
            if value.startswith(prefix): values.append(value)
        if not values: return None
        out = Path(self.output.edit.text()).expanduser(); out.mkdir(parents=True, exist_ok=True)
        path = out / name; path.write_text("\n".join(dict.fromkeys(values)) + "\n")
        return str(path)

    def _fill_fields(self, columns, ticked=None):
        """Show `columns`, ticking `ticked` (the published set by default)."""
        ticked = set(metadata_fields.DEFAULT_COLUMNS if ticked is None else ticked)
        self.fields.clear()
        for column in columns:
            item = QListWidgetItem(
                f"{column}  —  {metadata_fields.describe(column).description}")
            item.setData(Qt.UserRole, column)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if column in ticked else Qt.Unchecked)
            self.fields.addItem(item)

    def _load_fields(self):
        """Offer the columns the chosen database actually has."""
        path = self.database.edit.text().strip()
        if not path or not Path(path).expanduser().exists():
            QMessageBox.warning(self, "No database",
                                "Choose a GEOmetadb file first.")
            return
        columns = metadata_fields.available(str(Path(path).expanduser()))
        self._fill_fields(columns, ticked=self._chosen_fields() or None)

    def _chosen_fields(self):
        out = []
        for i in range(self.fields.count()):
            item = self.fields.item(i)
            if item.checkState() == Qt.Checked:
                out.append(item.data(Qt.UserRole))
        return out

    def command(self):
        required = [self.database.edit, self.output.edit]
        if self.phase.currentData() == "phase2":
            required += [self.vocab.edit, self.index.edit, self.cells.edit]
        if any(not field.text().strip() for field in required):
            raise ValueError("Choose all resources required by the selected stopping phase and an output directory.")
        labels = [name for name, box in self.labels.items() if box.isChecked()]
        if not labels: raise ValueError("Select at least one extraction field.")
        args = ["geo-label-extractor", "--input", self.database.edit.text(),
                "--out-dir", self.output.edit.text(),
                "--labels", ",".join(labels), "--stop-after", self.phase.currentData(),
                "--backend", self.backend.currentText(), "--llm-url", self.url.text(),
                "--phase1-model", self.p1_model.text(), "--age-model", self.age_model.text(),
                "--phase2-model", self.p2_model.text(), "--extract-workers",
                str(self.extract_workers.value()), "--normalize-workers",
                str(self.normalize_workers.value())]
        if self.phase.currentData() == "phase2":
            args += ["--vocab", self.vocab.edit.text(), "--index", self.index.edit.text(),
                     "--cellosaurus", self.cells.edit.text()]
        gsm = self._write_manifest("selected_gsms.txt", self.gsm_text.toPlainText(), "GSM")
        gpl = self._write_manifest("selected_gpls.txt", self.gpl_text.toPlainText(), "GPL")
        if gsm: args += ["--gsm-manifest", gsm]
        if gpl: args += ["--gpl-manifest", gpl]
        if self.tech.currentText().strip(): args += ["--tech", self.tech.currentText().strip()]
        if self.organism.currentText().strip(): args += ["--organism", self.organism.currentText().strip()]
        if self.spec_text.toPlainText().strip(): args += ["--spec", self.spec_text.toPlainText().strip()]
        fields = self._chosen_fields()
        if not fields:
            raise ValueError("Tick at least one metadata column for the model to read.")
        # Passing the default explicitly would be harmless but noisy; omitting
        # it keeps the published configuration the visibly plain case.
        if not metadata_fields.is_default(fields):
            args += ["--fields", ",".join(fields)]
        return args

    def preview_command(self):
        try: self.log.setPlainText(shlex.join(self.command()))
        except ValueError as exc: QMessageBox.warning(self, "Configuration", str(exc))

    def run(self):
        try: args = self.command()
        except ValueError as exc: QMessageBox.warning(self, "Configuration", str(exc)); return
        self.log.setPlainText("$ " + shlex.join(args) + "\n\n")
        self.nav.setCurrentRow(3)
        self.start.setEnabled(False); self.stop.setEnabled(True)
        self.process.start(args[0], args[1:])

    def read_output(self): self.log.appendPlainText(bytes(self.process.readAllStandardOutput()).decode(errors="replace").rstrip())
    def read_error(self): self.log.appendPlainText(bytes(self.process.readAllStandardError()).decode(errors="replace").rstrip())
    def finished(self, code, _status):
        self.log.appendPlainText(f"\nProcess finished with exit code {code}")
        self.start.setEnabled(True); self.stop.setEnabled(False)


def main() -> int:
    app = QApplication(sys.argv); app.setStyleSheet(STYLE)
    window = MainWindow(); window.show(); return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
