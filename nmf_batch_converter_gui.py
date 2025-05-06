import os
import sys
import json
import logging
import traceback
import threading
import configparser
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any, Union

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QListWidget, QFileDialog, QCheckBox,
    QProgressBar, QMessageBox, QGroupBox, QTabWidget, QTextEdit,
    QSplitter, QComboBox, QSpinBox, QLineEdit, QMenu, QAction,
    QSystemTrayIcon, QStyle, QListWidgetItem, QSlider, QDialog,
    QFormLayout, QRadioButton, QScrollArea, QToolBar, QStatusBar,
    QDockWidget, QFrame, QFontDialog, QColorDialog
)
from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QMimeData, QTimer, QSettings, QUrl,
    QSize, QPoint, QEvent, QObject, QDir, QFile, QIODevice
)
from PyQt5.QtGui import (
    QDragEnterEvent, QDropEvent, QIcon, QFont, QColor, QPalette,
    QKeySequence, QCursor, QDesktopServices, QPixmap, QDrag
)
import subprocess
import multiprocessing
from threading import Lock

# Setup logging
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler("nmf_converter.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("NMFConverter")

# Constants
VERSION = "2.0.0"
DEFAULT_CONFIG = {
    "General": {
        "theme": "system",
        "max_threads": str(max(1, multiprocessing.cpu_count() - 1)),
        "save_session": "True",
        "minimize_to_tray": "False",
        "check_updates": "True",
    },
    "Conversion": {
        "sample_rate": "8000",
        "channels": "1",
        "bit_depth": "16",
        "normalize_audio": "False",
        "normalize_level": "-1.0",
        "remove_silence": "False",
        "silence_threshold": "-60",
        "tmp_directory": "",
    },
    "UI": {
        "font_family": "Arial",
        "font_size": "9",
        "show_log_panel": "True",
        "show_preview_panel": "True",
        "window_width": "900",
        "window_height": "700",
    }
}


class AudioFormat:
    """Class representing an audio format with conversion settings"""
    def __init__(self, name, extension, codec=None, **kwargs):
        self.name = name
        self.extension = extension
        self.codec = codec
        self.params = kwargs
    
    def get_ffmpeg_args(self) -> List[str]:
        """Get FFmpeg command-line arguments for this format"""
        args = []
        if self.codec:
            args.extend(['-c:a', self.codec])
        
        for key, value in self.params.items():
            args.extend([f'-{key}', str(value)])
        
        return args


# Predefined output formats
OUTPUT_FORMATS = {
    "WAV (PCM)": AudioFormat("WAV (PCM)", ".wav", "pcm_s16le"),
    "WAV (24-bit)": AudioFormat("WAV (24-bit)", ".wav", "pcm_s24le"),
    "MP3": AudioFormat("MP3", ".mp3", "libmp3lame", q=4),
    "FLAC": AudioFormat("FLAC", ".flac", "flac"),
    "OGG": AudioFormat("OGG", ".ogg", "libvorbis", q=5),
    "AAC": AudioFormat("AAC", ".aac", "aac"),
}


class ConfigManager:
    """Manages application settings"""
    def __init__(self):
        self.config = configparser.ConfigParser()
        self.config_path = os.path.join(
            os.path.expanduser("~"), 
            ".nmf_converter_config.ini"
        )
        self.load_config()
    
    def load_config(self):
        """Load configuration from file or create default"""
        # Set up default configuration
        for section, options in DEFAULT_CONFIG.items():
            if not self.config.has_section(section):
                self.config.add_section(section)
            for option, value in options.items():
                if not self.config.has_option(section, option):
                    self.config.set(section, option, value)
        
        # Try to load existing config
        if os.path.exists(self.config_path):
            try:
                self.config.read(self.config_path)
                logger.info(f"Loaded configuration from {self.config_path}")
            except Exception as e:
                logger.error(f"Error loading config: {e}")
        else:
            self.save_config()
    
    def save_config(self):
        """Save configuration to file"""
        try:
            with open(self.config_path, 'w') as configfile:
                self.config.write(configfile)
            logger.info(f"Saved configuration to {self.config_path}")
        except Exception as e:
            logger.error(f"Error saving config: {e}")
    
    def get(self, section, option, fallback=None):
        """Get configuration value with type conversion"""
        try:
            value = self.config.get(section, option)
            # Try to convert to appropriate type
            if value.lower() in ('true', 'yes', 'on', '1'):
                return True
            elif value.lower() in ('false', 'no', 'off', '0'):
                return False
            try:
                return int(value)
            except ValueError:
                try:
                    return float(value)
                except ValueError:
                    return value
        except (configparser.NoSectionError, configparser.NoOptionError):
            return fallback

    def set(self, section, option, value):
        """Set configuration value with type conversion"""
        if not self.config.has_section(section):
            self.config.add_section(section)
        
        # Convert to string for storage
        if isinstance(value, bool):
            value_str = "True" if value else "False"
        else:
            value_str = str(value)
        
        self.config.set(section, option, value_str)
        self.save_config()


class ConversionTask:
    """Represents a single file conversion task"""
    def __init__(self, input_path, output_dir=None, output_format="WAV (PCM)", 
                 overwrite=True, extra_params=None):
        self.input_path = Path(input_path)
        self.output_dir = Path(output_dir) if output_dir else None
        self.output_format = output_format
        self.overwrite = overwrite
        self.extra_params = extra_params or {}
        self.status = "pending"  # pending, processing, completed, failed
        self.error = None
        self.progress = 0
        self.output_path = self._determine_output_path()
    
    def _determine_output_path(self):
        """Determine the output path based on format and settings"""
        format_info = OUTPUT_FORMATS[self.output_format]
        if self.output_dir:
            return self.output_dir / self.input_path.with_suffix(format_info.extension).name
        else:
            return self.input_path.with_suffix(format_info.extension)


class ConversionManager:
    """Manages the queue of conversion tasks"""
    def __init__(self, config_manager):
        self.config = config_manager
        self.tasks = []
        self.task_lock = Lock()
        self.workers = []
        self.paused = False
        self.canceled = False
    
    def add_task(self, task):
        """Add a task to the queue"""
        with self.task_lock:
            self.tasks.append(task)
    
    def add_tasks(self, tasks):
        """Add multiple tasks to the queue"""
        with self.task_lock:
            self.tasks.extend(tasks)
    
    def get_next_task(self):
        """Get the next pending task"""
        with self.task_lock:
            for task in self.tasks:
                if task.status == "pending":
                    task.status = "processing"
                    return task
        return None
    
    def start_conversion(self, progress_callback, finished_callback):
        """Start the conversion process with multiple workers"""
        self.paused = False
        self.canceled = False
        
        # Determine number of workers
        max_workers = self.config.get("Conversion", "max_threads")
        max_workers = min(max(1, int(max_workers)), len(self.tasks))
        
        # Create and start workers
        self.workers = []
        for i in range(max_workers):
            worker = ConversionWorker(self, self.config)
            worker.progress.connect(progress_callback)
            worker.finished.connect(finished_callback)
            self.workers.append(worker)
            worker.start()
    
    def cancel_conversion(self):
        """Cancel all ongoing conversions"""
        self.canceled = True
        for worker in self.workers:
            worker.canceled = True
    
    def pause_conversion(self):
        """Pause the conversion process"""
        self.paused = True
        for worker in self.workers:
            worker.paused = True
    
    def resume_conversion(self):
        """Resume the conversion process"""
        self.paused = False
        for worker in self.workers:
            worker.paused = False
    
    def get_stats(self):
        """Get conversion statistics"""
        total = len(self.tasks)
        pending = sum(1 for task in self.tasks if task.status == "pending")
        processing = sum(1 for task in self.tasks if task.status == "processing")
        completed = sum(1 for task in self.tasks if task.status == "completed")
        failed = sum(1 for task in self.tasks if task.status == "failed")
        
        return {
            "total": total,
            "pending": pending,
            "processing": processing,
            "completed": completed,
            "failed": failed,
            "progress": (completed + failed) / total if total > 0 else 0
        }


class ConversionWorker(QThread):
    """Worker thread for audio conversion"""
    progress = pyqtSignal(ConversionTask, float)  # task, progress percentage
    error = pyqtSignal(ConversionTask, str)       # task, error message
    finished = pyqtSignal(ConversionTask, bool)   # task, success
    
    def __init__(self, conversion_manager, config_manager):
        super().__init__()
        self.manager = conversion_manager
        self.config = config_manager
        self.canceled = False
        self.paused = False
        self.current_task = None
    
    def detect_codec(self, input_path):
        """Detect audio codec using ffprobe"""
        try:
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-select_streams', 'a:0',
                '-show_entries', 'stream=codec_name,duration,sample_rate,channels',
                '-of', 'json',
                str(input_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            
            if 'streams' in data and data['streams']:
                return data['streams'][0]
            return None
        except Exception as e:
            logger.error(f"Codec detection failed: {str(e)}")
            raise RuntimeError(f"Codec detection failed: {str(e)}")
    
    def run(self):
        """Process tasks from the queue"""
        while not self.canceled:
            # Check for pause
            if self.paused:
                self.sleep(1)
                continue
            
            # Get next task
            self.current_task = self.manager.get_next_task()
            if not self.current_task:
                break  # No more tasks
            
            try:
                self.process_task(self.current_task)
            except Exception as e:
                logger.error(f"Error processing {self.current_task.input_path}: {str(e)}")
                self.current_task.status = "failed"
                self.current_task.error = str(e)
                self.error.emit(self.current_task, str(e))
                self.finished.emit(self.current_task, False)
    
    def process_task(self, task):
        """Process a single conversion task"""
        input_path = task.input_path
        output_path = task.output_path
        
        # Skip if output exists and not overwriting
        if output_path.exists() and not task.overwrite:
            logger.info(f"Skipping {input_path} - output exists")
            task.status = "completed"
            self.progress.emit(task, 100)
            self.finished.emit(task, True)
            return
        
        try:
            # Ensure output directory exists
            if not output_path.parent.exists():
                output_path.parent.mkdir(parents=True)
            
            # Get audio info
            audio_info = self.detect_codec(input_path)
            if not audio_info:
                raise RuntimeError("Could not detect audio stream")
            
            # Get format settings
            format_info = OUTPUT_FORMATS[task.output_format]
            
            # Build FFmpeg command
            cmd = [
                'ffmpeg',
                '-y',  # Overwrite output
                '-i', str(input_path),
            ]
            
            # Add format-specific parameters
            cmd.extend(format_info.get_ffmpeg_args())
            
            # Add sample rate if specified
            sample_rate = self.config.get("Conversion", "sample_rate")
            if sample_rate:
                cmd.extend(['-ar', str(sample_rate)])
            
            # Add channels if specified
            channels = self.config.get("Conversion", "channels")
            if channels:
                cmd.extend(['-ac', str(channels)])
            
            # Add normalization if enabled
            if self.config.get("Conversion", "normalize_audio"):
                level = self.config.get("Conversion", "normalize_level", "-1.0")
                cmd.extend(['-af', f'loudnorm=I={level}:TP=-1.5:LRA=11'])
            
            # Add silence removal if enabled
            if self.config.get("Conversion", "remove_silence"):
                threshold = self.config.get("Conversion", "silence_threshold", "-60")
                cmd.extend(['-af', f'silenceremove=stop_threshold={threshold}dB'])
            
            # Add any extra parameters from the task
            for key, value in task.extra_params.items():
                cmd.extend([f'-{key}', str(value)])
            
            # Add output path
            cmd.append(str(output_path))
            
            # Run FFmpeg with progress monitoring
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1
            )
            
            # Parse progress
            duration = float(audio_info.get('duration', 0))
            if duration > 0:
                for line in process.stderr:
                    if self.canceled:
                        process.terminate()
                        break
                    
                    if "time=" in line:
                        time_parts = line.split("time=")[1].split()[0].split(":")
                        if len(time_parts) == 3:
                            hours, minutes, seconds = time_parts
                            time_seconds = (
                                int(hours) * 3600 + 
                                int(minutes) * 60 + 
                                float(seconds)
                            )
                            progress = min(100, (time_seconds / duration) * 100)
                            self.progress.emit(task, progress)
            
            # Wait for process to complete
            process.wait()
            
            # Check result
            if process.returncode != 0 and not self.canceled:
                raise RuntimeError(f"FFmpeg failed with return code {process.returncode}")
            
            # Success
            if not self.canceled:
                task.status = "completed"
                self.progress.emit(task, 100)
                self.finished.emit(task, True)
            
        except Exception as e:
            logger.error(f"Conversion failed for {input_path}: {str(e)}")
            task.status = "failed"
            task.error = str(e)
            self.error.emit(task, str(e))
            self.finished.emit(task, False)


class AudioPlayer:
    """Simple audio player using ffplay"""
    def __init__(self):
        self.process = None
        self.is_playing = False
    
    def play(self, file_path):
        """Play an audio file"""
        if self.is_playing:
            self.stop()
        
        try:
            cmd = [
                'ffplay',
                '-nodisp',      # No display
                '-autoexit',    # Exit when done
                '-loglevel', 'quiet',  # No logging
                str(file_path)
            ]
            self.process = subprocess.Popen(cmd)
            self.is_playing = True
            return True
        except Exception as e:
            logger.error(f"Error playing audio: {e}")
            return False
    
    def stop(self):
        """Stop playback"""
        if self.process:
            try:
                self.process.terminate()
                self.process = None
            except Exception as e:
                logger.error(f"Error stopping playback: {e}")
            
            self.is_playing = False


class SettingsDialog(QDialog):
    """Dialog for application settings"""
    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.config = config_manager
        self.setWindowTitle("Settings")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)
        
        self.init_ui()
    
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Create tabs
        tabs = QTabWidget()
        layout.addWidget(tabs)
        
        # General tab
        general_tab = QWidget()
        general_layout = QFormLayout(general_tab)
        
        # Theme selection
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["System", "Light", "Dark"])
        current_theme = self.config.get("General", "theme", "system").lower()
        self.theme_combo.setCurrentText(current_theme.capitalize())
        general_layout.addRow("Theme:", self.theme_combo)
        
        # Max threads
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, multiprocessing.cpu_count())
        self.threads_spin.setValue(self.config.get("General", "max_threads", 2))
        general_layout.addRow("Maximum threads:", self.threads_spin)
        
        # Save session
        self.save_session_cb = QCheckBox()
        self.save_session_cb.setChecked(self.config.get("General", "save_session", True))
        general_layout.addRow("Save session on exit:", self.save_session_cb)
        
        # Minimize to tray
        self.minimize_tray_cb = QCheckBox()
        self.minimize_tray_cb.setChecked(self.config.get("General", "minimize_to_tray", False))
        general_layout.addRow("Minimize to system tray:", self.minimize_tray_cb)
        
        # Check for updates
        self.check_updates_cb = QCheckBox()
        self.check_updates_cb.setChecked(self.config.get("General", "check_updates", True))
        general_layout.addRow("Check for updates on startup:", self.check_updates_cb)
        
        tabs.addTab(general_tab, "General")
        
        # Conversion tab
        conversion_tab = QWidget()
        conversion_layout = QFormLayout(conversion_tab)
        
        # Sample rate
        self.sample_rate_combo = QComboBox()
        self.sample_rate_combo.addItems(["8000", "16000", "22050", "44100", "48000", "96000"])
        self.sample_rate_combo.setCurrentText(str(self.config.get("Conversion", "sample_rate", "8000")))
        conversion_layout.addRow("Sample rate (Hz):", self.sample_rate_combo)
        
        # Channels
        self.channels_combo = QComboBox()
        self.channels_combo.addItems(["1", "2"])
        self.channels_combo.setCurrentText(str(self.config.get("Conversion", "channels", "1")))
        conversion_layout.addRow("Channels:", self.channels_combo)
        
        # Bit depth
        self.bit_depth_combo = QComboBox()
        self.bit_depth_combo.addItems(["16", "24", "32"])
        self.bit_depth_combo.setCurrentText(str(self.config.get("Conversion", "bit_depth", "16")))
        conversion_layout.addRow("Bit depth:", self.bit_depth_combo)
        
        # Normalize audio
        self.normalize_cb = QCheckBox()
        self.normalize_cb.setChecked(self.config.get("Conversion", "normalize_audio", False))
        conversion_layout.addRow("Normalize audio:", self.normalize_cb)
        
        # Normalize level
        self.normalize_level = QLineEdit()
        self.normalize_level.setText(str(self.config.get("Conversion", "normalize_level", "-1.0")))
        conversion_layout.addRow("Normalization level (dB):", self.normalize_level)
        
        # Remove silence
        self.remove_silence_cb = QCheckBox()
        self.remove_silence_cb.setChecked(self.config.get("Conversion", "remove_silence", False))
        conversion_layout.addRow("Remove silence:", self.remove_silence_cb)
        
        # Silence threshold
        self.silence_threshold = QLineEdit()
        self.silence_threshold.setText(str(self.config.get("Conversion", "silence_threshold", "-60")))
        conversion_layout.addRow("Silence threshold (dB):", self.silence_threshold)
        
        tabs.addTab(conversion_tab, "Conversion")
        
        # UI tab
        ui_tab = QWidget()
        ui_layout = QFormLayout(ui_tab)
        
        # Font selection
        self.font_btn = QPushButton("Select Font...")
        self.font_btn.clicked.connect(self.select_font)
        current_font = QFont(
            self.config.get("UI", "font_family", "Arial"),
            self.config.get("UI", "font_size", 9)
        )
        self.selected_font = current_font
        self.font_label = QLabel(f"{current_font.family()}, {current_font.pointSize()}pt")
        font_layout = QHBoxLayout()
        font_layout.addWidget(self.font_label)
        font_layout.addWidget(self.font_btn)
        ui_layout.addRow("Font:", font_layout)
        
        # Show log panel
        self.show_log_cb = QCheckBox()
        self.show_log_cb.setChecked(self.config.get("UI", "show_log_panel", True))
        ui_layout.addRow("Show log panel:", self.show_log_cb)
        
        # Show preview panel
        self.show_preview_cb = QCheckBox()
        self.show_preview_cb.setChecked(self.config.get("UI", "show_preview_panel", True))
        ui_layout.addRow("Show preview panel:", self.show_preview_cb)
        
        tabs.addTab(ui_tab, "Interface")
        
        # Buttons
        button_layout = QHBoxLayout()
        
        # Reset button
        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.clicked.connect(self.reset_settings)
        button_layout.addWidget(reset_btn)
        
        button_layout.addStretch()
        
        # Cancel button
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        # Save button
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.save_settings)
        save_btn.setDefault(True)
        button_layout.addWidget(save_btn)
        
        layout.addLayout(button_layout)
    
    def select_font(self):
        """Open font selection dialog"""
        font, ok = QFontDialog.getFont(self.selected_font, self)
        if ok:
            self.selected_font = font
            self.font_label.setText(f"{font.family()}, {font.pointSize()}pt")
    
    def reset_settings(self):
        """Reset settings to defaults"""
        result = QMessageBox.question(
            self, "Reset Settings",
            "Are you sure you want to reset all settings to their defaults?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if result == QMessageBox.Yes:
            # Reset to defaults
            for section, options in DEFAULT_CONFIG.items():
                for option, value in options.items():
                    self.config.set(section, option, value)
            
            # Update UI
            self.reject()  # Close dialog
            QMessageBox.information(self, "Settings Reset", 
                                   "Settings have been reset to defaults. Restart the application for all changes to take effect.")
    
    def save_settings(self):
        """Save settings from dialog to config"""
        # General tab
        self.config.set("General", "theme", self.theme_combo.currentText().lower())
        self.config.set("General", "max_threads", self.threads_spin.value())
        self.config.set("General", "save_session", self.save_session_cb.isChecked())
        self.config.set("General", "minimize_to_tray", self.minimize_tray_cb.isChecked())
        self.config.set("General", "check_updates", self.check_updates_cb.isChecked())
        
        # Conversion tab
        self.config.set("Conversion", "sample_rate", self.sample_rate_combo.currentText())
        self.config.set("Conversion", "channels", self.channels_combo.currentText())
        self.config.set("Conversion", "bit_depth", self.bit_depth_combo.currentText())
        self.config.set("Conversion", "normalize_audio", self.normalize_cb.isChecked())
        self.config.set("Conversion", "normalize_level", self.normalize_level.text())
        self.config.set("Conversion", "remove_silence", self.remove_silence_cb.isChecked())
        self.config.set("Conversion", "silence_threshold", self.silence_threshold.text())
        
        # UI tab
        self.config.set("UI", "font_family", self.selected_font.family())
        self.config.set("UI", "font_size", self.selected_font.pointSize())
        self.config.set("UI", "show_log_panel", self.show_log_cb.isChecked())
        self.config.set("UI", "show_preview_panel", self.show_preview_cb.isChecked())
        
        self.accept()


class AboutDialog(QDialog):
    """About dialog with version and credits"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About NMF to WAV Converter")
        self.setMinimumWidth(400)
        
        layout = QVBoxLayout(self)
        
        # Title and version
        title_label = QLabel(f"NMF to WAV Converter v{VERSION}")
        title_label.setAlignment(Qt.AlignCenter)
        font = title_label.font()
        font.setPointSize(14)
        font.setBold(True)
        title_label.setFont(font)
        layout.addWidget(title_label)
        
        # Description
        desc_label = QLabel(
            "A powerful tool for converting audio files to different formats, "
            "with a focus on NMF to WAV conversion."
        )
        desc_label.setWordWrap(True)
        desc_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(desc_label)
        
        # Line separator
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)
        
        # Dependencies
        deps_label = QLabel("Using:")
        deps_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(deps_label)
        
        deps_text = QLabel(
            "- FFmpeg for audio conversion\n"
            "- PyQt5 for the user interface\n"
            "- Python 3.8+"
        )
        deps_text.setAlignment(Qt.AlignCenter)
        layout.addWidget(deps_text)
        
        # Line separator
        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line2)
        
        # Copyright
        copy_label = QLabel("© 2025")
        copy_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(copy_label)
        
        # License
        license_label = QLabel("Released under MIT License")
        license_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(license_label)
        
        # Close button
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)


class PreviewPanel(QWidget):
    """Audio preview panel with waveform display and controls"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_file = None
        self.player = AudioPlayer()
        
        self.init_ui()
    
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # File info section
        info_group = QGroupBox("File Information")
        info_layout = QFormLayout(info_group)
        
        self.file_label = QLabel("No file selected")
        info_layout.addRow("File:", self.file_label)
        
        self.format_label = QLabel("-")
        info_layout.addRow("Format:", self.format_label)
        
        self.codec_label = QLabel("-")
        info_layout.addRow("Codec:", self.codec_label)
        
        self.sample_rate_label = QLabel("-")
        info_layout.addRow("Sample Rate:", self.sample_rate_label)
        
        self.channels_label = QLabel("-")
        info_layout.addRow("Channels:", self.channels_label)
        
        self.duration_label = QLabel("-")
        info_layout.addRow("Duration:", self.duration_label)
        
        layout.addWidget(info_group)
        
        # Playback controls
        controls_layout = QHBoxLayout()
        
        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self.play_audio)
        self.play_btn.setEnabled(False)
        controls_layout.addWidget(self.play_btn)
        
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_audio)
        self.stop_btn.setEnabled(False)
        controls_layout.addWidget(self.stop_btn)
        
        layout.addLayout(controls_layout)
        
        # Add a placeholder for waveform visualization
        self.waveform_label = QLabel("Waveform visualization not available")
        self.waveform_label.setAlignment(Qt.AlignCenter)
        self.waveform_label.setStyleSheet("background-color: #f0f0f0; padding: 50px;")
        layout.addWidget(self.waveform_label)
        
        layout.addStretch()
    
    def set_file(self, file_path):
        """Set the file to preview"""
        if not file_path or not Path(file_path).exists():
            self.clear()
            return
        
        self.current_file = file_path
        file_name = Path(file_path).name
        self.file_label.setText(file_name)
        
        # Get file info using ffprobe
        try:
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-select_streams', 'a:0',
                '-show_entries', 'stream=codec_name,sample_rate,channels,duration,bit_rate',
                '-show_entries', 'format=duration',
                '-of', 'json',
                str(file_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            
            if 'streams' in data and data['streams']:
                stream = data['streams'][0]
                self.codec_label.setText(stream.get('codec_name', 'Unknown'))
                self.sample_rate_label.setText(f"{stream.get('sample_rate', 'Unknown')} Hz")
                self.channels_label.setText(str(stream.get('channels', 'Unknown')))
                
                # Get duration and format it
                duration = stream.get('duration') or data.get('format', {}).get('duration')
                if duration:
                    duration = float(duration)
                    minutes = int(duration // 60)
                    seconds = int(duration % 60)
                    self.duration_label.setText(f"{minutes:02d}:{seconds:02d}")
                else:
                    self.duration_label.setText("Unknown")
                
                # Set format based on file extension
                self.format_label.setText(file_path.split('.')[-1].upper())
            
            # Enable playback
            self.play_btn.setEnabled(True)
            
        except Exception as e:
            logger.error(f"Error getting file info: {e}")
            # Show error in info fields
            self.codec_label.setText("Error")
            self.sample_rate_label.setText("Error")
            self.channels_label.setText("Error")
            self.duration_label.setText("Error")
    
    def clear(self):
        """Clear preview panel"""
        self.current_file = None
        self.file_label.setText("No file selected")
        self.codec_label.setText("-")
        self.sample_rate_label.setText("-")
        self.channels_label.setText("-")
        self.duration_label.setText("-")
        self.format_label.setText("-")
        self.play_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.stop_audio()
    
    def play_audio(self):
        """Play the current audio file"""
        if not self.current_file:
            return
        
        if self.player.play(self.current_file):
            self.play_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
    
    def stop_audio(self):
        """Stop audio playback"""
        self.player.stop()
        self.play_btn.setEnabled(self.current_file is not None)
        self.stop_btn.setEnabled(False)


class CustomListWidgetItem(QListWidgetItem):
    """Custom list widget item for file display with status"""
    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task
        self.update_display()
    
    def update_display(self):
        """Update the display based on task status"""
        self.setText(self.task.input_path.name)
        
        if self.task.status == "pending":
            self.setIcon(QApplication.style().standardIcon(QStyle.SP_FileIcon))
        elif self.task.status == "processing":
            self.setIcon(QApplication.style().standardIcon(QStyle.SP_BrowserReload))
        elif self.task.status == "completed":
            self.setIcon(QApplication.style().standardIcon(QStyle.SP_DialogApplyButton))
        elif self.task.status == "failed":
            self.setIcon(QApplication.style().standardIcon(QStyle.SP_DialogCancelButton))


class LogPanel(QWidget):
    """Log display panel"""
    def __init__(self, parent=None):
        super().__init__(parent)
        
        self.init_ui()
        
        # Set up log capture
        self.log_handler = LogHandler(self.append_log)
        self.log_handler.setLevel(logging.INFO)
        self.log_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(self.log_handler)
    
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Log display
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        layout.addWidget(self.log_display)
        
        # Control buttons
        btn_layout = QHBoxLayout()
        
        self.clear_btn = QPushButton("Clear Log")
        self.clear_btn.clicked.connect(self.clear_log)
        btn_layout.addWidget(self.clear_btn)
        
        self.copy_btn = QPushButton("Copy to Clipboard")
        self.copy_btn.clicked.connect(self.copy_to_clipboard)
        btn_layout.addWidget(self.copy_btn)
        
        self.save_btn = QPushButton("Save Log")
        self.save_btn.clicked.connect(self.save_log)
        btn_layout.addWidget(self.save_btn)
        
        layout.addLayout(btn_layout)
    
    def append_log(self, message):
        """Append a message to the log display"""
        self.log_display.append(message)
        # Auto-scroll to bottom
        scrollbar = self.log_display.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
    
    def clear_log(self):
        """Clear the log display"""
        self.log_display.clear()
    
    def copy_to_clipboard(self):
        """Copy log contents to clipboard"""
        QApplication.clipboard().setText(self.log_display.toPlainText())
    
    def save_log(self):
        """Save log to file"""
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save Log", "", "Log Files (*.log);;Text Files (*.txt);;All Files (*.*)"
        )
        
        if file_path:
            try:
                with open(file_path, 'w') as f:
                    f.write(self.log_display.toPlainText())
                self.append_log(f"Log saved to {file_path}")
            except Exception as e:
                logger.error(f"Error saving log: {e}")
                QMessageBox.warning(self, "Error", f"Could not save log: {str(e)}")


class LogHandler(logging.Handler):
    """Custom log handler to redirect logs to the UI"""
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
    
    def emit(self, record):
        """Emit a log record"""
        log_message = self.format(record)
        # Use invokeMethod to safely call from any thread
        QTimer.singleShot(0, lambda: self.callback(log_message))


class NMFBatchConverterGUI(QMainWindow):
    """Main application window"""
    def __init__(self):
        super().__init__()
        
        # Initialize configuration
        self.config_manager = ConfigManager()
        
        # Initialize conversion manager
        self.conversion_manager = ConversionManager(self.config_manager)
        
        # Set up UI
        self.setWindowTitle(f"NMF to WAV Converter v{VERSION}")
        self.resize(
            self.config_manager.get("UI", "window_width", 900),
            self.config_manager.get("UI", "window_height", 700)
        )
        
        # Set up central widget
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        
        # Set up the main layout
        self.main_layout = QVBoxLayout(self.central_widget)
        
        # Create UI components
        self.create_menu_bar()
        self.create_tool_bar()
        self.create_status_bar()
        self.create_main_interface()
        
        # Apply font
        self.apply_font()
        
        # Set up system tray if enabled
        if self.config_manager.get("General", "minimize_to_tray", False):
            self.setup_system_tray()
        
        # Verify FFmpeg/ffprobe
        self.check_ffmpeg_installation()
        
        # Apply theme
        self.apply_theme()
        
        # Load session if enabled
        if self.config_manager.get("General", "save_session", True):
            self.load_session()
        
        # Check for updates if enabled
        if self.config_manager.get("General", "check_updates", True):
            QTimer.singleShot(1000, self.check_for_updates)
        
        logger.info(f"Application started - v{VERSION}")
    
    def apply_font(self):
        """Apply configured font to application"""
        font_family = self.config_manager.get("UI", "font_family", "Arial")
        font_size = self.config_manager.get("UI", "font_size", 9)
        
        font = QFont(font_family, font_size)
        QApplication.setFont(font)
    
    def apply_theme(self):
        """Apply the selected theme"""
        theme = self.config_manager.get("General", "theme", "system").lower()
        
        if theme == "dark":
            self.set_dark_theme()
        elif theme == "light":
            self.set_light_theme()
        # System theme is handled by QApplication.setStyle()
    
    def set_dark_theme(self):
        """Apply dark theme to application"""
        palette = QPalette()
        
        # Dark color scheme
        palette.setColor(QPalette.Window, QColor(53, 53, 53))
        palette.setColor(QPalette.WindowText, Qt.white)
        palette.setColor(QPalette.Base, QColor(25, 25, 25))
        palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
        palette.setColor(QPalette.ToolTipBase, Qt.white)
        palette.setColor(QPalette.ToolTipText, Qt.white)
        palette.setColor(QPalette.Text, Qt.white)
        palette.setColor(QPalette.Button, QColor(53, 53, 53))
        palette.setColor(QPalette.ButtonText, Qt.white)
        palette.setColor(QPalette.BrightText, Qt.red)
        palette.setColor(QPalette.Link, QColor(42, 130, 218))
        palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
        palette.setColor(QPalette.HighlightedText, Qt.black)
        
        QApplication.setPalette(palette)
    
    def set_light_theme(self):
        """Apply light theme to application"""
        QApplication.setPalette(QApplication.style().standardPalette())
    
    def setup_system_tray(self):
        """Set up system tray icon and menu"""
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QApplication.style().standardIcon(QStyle.SP_MediaPlay))
        
        # Create tray menu
        tray_menu = QMenu()
        
        show_action = QAction("Show", self)
        show_action.triggered.connect(self.show)
        tray_menu.addAction(show_action)
        
        hide_action = QAction("Hide", self)
        hide_action.triggered.connect(self.hide)
        tray_menu.addAction(hide_action)
        
        tray_menu.addSeparator()
        
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.quit)
        tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()
        
        # Connect activation signal
        self.tray_icon.activated.connect(self.tray_icon_activated)
    
    def tray_icon_activated(self, reason):
        """Handle tray icon activation"""
        if reason == QSystemTrayIcon.DoubleClick:
            if self.isVisible():
                self.hide()
            else:
                self.show()
                self.activateWindow()
    
    def closeEvent(self, event):
        """Handle window close event"""
        if self.config_manager.get("General", "minimize_to_tray", False) and hasattr(self, 'tray_icon'):
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                "NMF to WAV Converter",
                "Application is still running in the system tray.",
                QSystemTrayIcon.Information,
                2000
            )
        else:
            # Save session if enabled
            if self.config_manager.get("General", "save_session", True):
                self.save_session()
            
            # Save window size
            self.config_manager.set("UI", "window_width", self.width())
            self.config_manager.set("UI", "window_height", self.height())
            
            # Accept the close event
            event.accept()
    
    def check_ffmpeg_installation(self):
        """Verify ffmpeg and ffprobe are available"""
        try:
            subprocess.run(['ffmpeg', '-version'], check=True, 
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(['ffprobe', '-version'], check=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Get FFmpeg version for logging
            result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
            version_line = result.stdout.split('\n')[0]
            logger.info(f"FFmpeg detected: {version_line}")
            
        except Exception as e:
            logger.error(f"FFmpeg check failed: {e}")
            QMessageBox.critical(
                self, "Dependency Missing",
                "FFmpeg/ffprobe not found!\n\n"
                "Please install FFmpeg and ensure it's in your PATH.\n"
                "Windows: Download from ffmpeg.org\n"
                "macOS: brew install ffmpeg\n"
                "Linux: sudo apt install ffmpeg"
            )
            sys.exit(1)
    
    def check_for_updates(self):
        """Check for application updates"""
        # This is a simplified placeholder for update checking
        # In a real application, this would connect to a server
        logger.info("Checking for updates...")
        # Simulated check - in a real app, this would make a network request
        # and compare versions
        self.statusBar().showMessage("No updates available", 3000)
    
    def save_session(self):
        """Save current session (file list, settings)"""
        try:
            session_file = os.path.join(
                os.path.expanduser("~"),
                ".nmf_converter_session.json"
            )
            
            # Get file list
            files = []
            for i in range(self.file_list.count()):
                item = self.file_list.item(i)
                if isinstance(item, CustomListWidgetItem):
                    task = item.task
                    files.append({
                        "input_path": str(task.input_path),
                        "status": task.status
                    })
            
            # Create session data
            session_data = {
                "files": files,
                "output_dir": self.output_dir,
                "output_format": self.format_combo.currentText()
            }
            
            # Save to file
            with open(session_file, 'w') as f:
                json.dump(session_data, f, indent=2)
            
            logger.info(f"Session saved to {session_file}")
            
        except Exception as e:
            logger.error(f"Error saving session: {e}")
    
    def load_session(self):
        """Load previous session"""
        try:
            session_file = os.path.join(
                os.path.expanduser("~"),
                ".nmf_converter_session.json"
            )
            
            if not os.path.exists(session_file):
                return
            
            with open(session_file, 'r') as f:
                session_data = json.load(f)
            
            # Restore file list
            files = session_data.get("files", [])
            for file_data in files:
                input_path = file_data.get("input_path")
                status = file_data.get("status", "pending")
                
                if input_path and Path(input_path).exists():
                    task = ConversionTask(
                        input_path,
                        self.output_dir,
                        session_data.get("output_format", "WAV (PCM)"),
                        True
                    )
                    task.status = status
                    item = CustomListWidgetItem(task)
                    self.file_list.addItem(item)
            
            # Restore output directory
            output_dir = session_data.get("output_dir")
            if output_dir and os.path.exists(output_dir):
                self.output_dir = output_dir
                self.output_label.setText(output_dir)
                self.output_label.setStyleSheet("")
            
            # Restore output format
            output_format = session_data.get("output_format")
            if output_format and output_format in OUTPUT_FORMATS:
                self.format_combo.setCurrentText(output_format)
            
            logger.info(f"Session loaded from {session_file}")
            
        except Exception as e:
            logger.error(f"Error loading session: {e}")
    
    def create_menu_bar(self):
        """Create application menu bar"""
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu('&File')
        
        add_files_action = QAction('Add Files...', self)
        add_files_action.setShortcut('Ctrl+O')
        add_files_action.triggered.connect(self.add_files)
        file_menu.addAction(add_files_action)
        
        add_folder_action = QAction('Add Folder...', self)
        add_folder_action.setShortcut('Ctrl+Shift+O')
        add_folder_action.triggered.connect(self.add_folder)
        file_menu.addAction(add_folder_action)
        
        file_menu.addSeparator()
        
        save_list_action = QAction('Save File List...', self)
        save_list_action.setShortcut('Ctrl+S')
        save_list_action.triggered.connect(self.save_file_list)
        file_menu.addAction(save_list_action)
        
        load_list_action = QAction('Load File List...', self)
        load_list_action.setShortcut('Ctrl+L')
        load_list_action.triggered.connect(self.load_file_list)
        file_menu.addAction(load_list_action)
        
        file_menu.addSeparator()
        
        settings_action = QAction('Settings...', self)
        settings_action.triggered.connect(self.show_settings)
        file_menu.addAction(settings_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction('Exit', self)
        exit_action.setShortcut('Ctrl+Q')
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # Edit menu
        edit_menu = menubar.addMenu('&Edit')
        
        select_all_action = QAction('Select All', self)
        select_all_action.setShortcut('Ctrl+A')
        select_all_action.triggered.connect(self.select_all_files)
        edit_menu.addAction(select_all_action)
        
        clear_selection_action = QAction('Clear Selection', self)
        clear_selection_action.setShortcut('Ctrl+D')
        clear_selection_action.triggered.connect(self.clear_selection)
        edit_menu.addAction(clear_selection_action)
        
        edit_menu.addSeparator()
        
        remove_selected_action = QAction('Remove Selected', self)
        remove_selected_action.setShortcut('Delete')
        remove_selected_action.triggered.connect(self.remove_selected)
        edit_menu.addAction(remove_selected_action)
        
        clear_list_action = QAction('Clear List', self)
        clear_list_action.triggered.connect(self.clear_files)
        edit_menu.addAction(clear_list_action)
        
        # View menu
        view_menu = menubar.addMenu('&View')
        
        # Toggle log panel
        self.show_log_action = QAction('Show Log Panel', self)
        self.show_log_action.setCheckable(True)
        self.show_log_action.setChecked(self.config_manager.get("UI", "show_log_panel", True))
        self.show_log_action.triggered.connect(self.toggle_log_panel)
        view_menu.addAction(self.show_log_action)
        
        # Toggle preview panel
        self.show_preview_action = QAction('Show Preview Panel', self)
        self.show_preview_action.setCheckable(True)
        self.show_preview_action.setChecked(self.config_manager.get("UI", "show_preview_panel", True))
        self.show_preview_action.triggered.connect(self.toggle_preview_panel)
        view_menu.addAction(self.show_preview_action)
        
        # Help menu
        help_menu = menubar.addMenu('&Help')
        
        about_action = QAction('About', self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
        
        check_updates_action = QAction('Check for Updates', self)
        check_updates_action.triggered.connect(self.check_for_updates)
        help_menu.addAction(check_updates_action)
    
    def create_tool_bar(self):
        """Create application toolbar"""
        toolbar = QToolBar("Main Toolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(24, 24))
        self.addToolBar(toolbar)
        
        # Add files button
        add_files_btn = QAction(
            QApplication.style().standardIcon(QStyle.SP_FileIcon),
            "Add Files", self
        )
        add_files_btn.triggered.connect(self.add_files)
        toolbar.addAction(add_files_btn)
        
        # Add folder button
        add_folder_btn = QAction(
            QApplication.style().standardIcon(QStyle.SP_DirIcon),
            "Add Folder", self
        )
        add_folder_btn.triggered.connect(self.add_folder)
        toolbar.addAction(add_folder_btn)
        
        toolbar.addSeparator()
        
        # Convert button
        self.convert_btn = QAction(
            QApplication.style().standardIcon(QStyle.SP_MediaPlay),
            "Start Conversion", self
        )
        self.convert_btn.triggered.connect(self.start_conversion)
        toolbar.addAction(self.convert_btn)
        
        # Pause button
        self.pause_btn = QAction(
            QApplication.style().standardIcon(QStyle.SP_MediaPause),
            "Pause", self
        )
        self.pause_btn.triggered.connect(self.toggle_pause)
        self.pause_btn.setEnabled(False)
        toolbar.addAction(self.pause_btn)
        
        # Cancel button
        self.cancel_btn = QAction(
            QApplication.style().standardIcon(QStyle.SP_MediaStop),
            "Cancel", self
        )
        self.cancel_btn.triggered.connect(self.cancel_conversion)
        self.cancel_btn.setEnabled(False)
        toolbar.addAction(self.cancel_btn)
        
        toolbar.addSeparator()
        
        # Settings button
        settings_btn = QAction(
            QApplication.style().standardIcon(QStyle.SP_FileDialogDetailedView),
            "Settings", self
        )
        settings_btn.triggered.connect(self.show_settings)
        toolbar.addAction(settings_btn)
    
    def create_status_bar(self):
        """Create application status bar"""
        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        
        # Add status label
        self.status_label = QLabel("Ready")
        status_bar.addWidget(self.status_label, 1)
        
        # Add progress bar
        self.status_progress = QProgressBar()
        self.status_progress.setMaximumWidth(150)
        self.status_progress.setMaximumHeight(15)
        self.status_progress.setValue(0)
        status_bar.addPermanentWidget(self.status_progress)
    
    def create_main_interface(self):
        """Create the main interface layout"""
        # Create main splitter
        self.main_splitter = QSplitter(Qt.Vertical)
        
        # Create top panel
        top_panel = QWidget()
        top_layout = QVBoxLayout(top_panel)
        top_layout.setContentsMargins(5, 5, 5, 5)
        
        # File selection group
        self.create_file_selection_group(top_layout)
        
        # Options group
        self.create_options_group(top_layout)
        
        self.main_splitter.addWidget(top_panel)
        
        # Create bottom panel with tabs
        self.bottom_tabs = QTabWidget()
        
        # Preview panel
        self.preview_panel = PreviewPanel()
        self.bottom_tabs.addTab(self.preview_panel, "Preview")
        
        # Log panel
        self.log_panel = LogPanel()
        self.bottom_tabs.addTab(self.log_panel, "Log")
        
        self.main_splitter.addWidget(self.bottom_tabs)
        
        # Set initial splitter sizes
        self.main_splitter.setSizes([int(self.height() * 0.7), int(self.height() * 0.3)])
        
        # Add main splitter to layout
        self.main_layout.addWidget(self.main_splitter)
        
        # Show/hide panels based on config
        if not self.config_manager.get("UI", "show_log_panel", True):
            self.toggle_log_panel()
        
        if not self.config_manager.get("UI", "show_preview_panel", True):
            self.toggle_preview_panel()
    
    def create_file_selection_group(self, parent_layout=None):
        """Create the file selection group box"""
        group_box = QGroupBox("File Selection")
        layout = QVBoxLayout(group_box)
        
        # File list
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.file_list.setAcceptDrops(True)
        self.file_list.setDragDropMode(QListWidget.DragDrop)
        self.file_list.setDefaultDropAction(Qt.CopyAction)
        self.file_list.itemSelectionChanged.connect(self.on_file_selection_changed)
        
        # Enable drag and drop
        self.file_list.__class__.dragEnterEvent = self.list_drag_enter_event
        self.file_list.__class__.dropEvent = self.list_drop_event
        
        layout.addWidget(self.file_list)
        
        # File selection buttons
        btn_layout = QHBoxLayout()
        
        self.add_files_btn = QPushButton("Add Files")
        self.add_files_btn.clicked.connect(self.add_files)
        btn_layout.addWidget(self.add_files_btn)
        
        self.add_folder_btn = QPushButton("Add Folder")
        self.add_folder_btn.clicked.connect(self.add_folder)
        btn_layout.addWidget(self.add_folder_btn)
        
        self.remove_btn = QPushButton("Remove Selected")
        self.remove_btn.clicked.connect(self.remove_selected)
        btn_layout.addWidget(self.remove_btn)
        
        self.clear_btn = QPushButton("Clear All")
        self.clear_btn.clicked.connect(self.clear_files)
        btn_layout.addWidget(self.clear_btn)
        
        layout.addLayout(btn_layout)
        
        # Add to parent layout if provided, otherwise add to main layout
        if parent_layout:
            parent_layout.addWidget(group_box)
        else:
            self.main_layout.addWidget(group_box)
    
    def create_options_group(self, parent_layout=None):
        """Create the options group box"""
        group_box = QGroupBox("Conversion Options")
        layout = QVBoxLayout(group_box)
        
        # Two-column layout for options
        options_layout = QHBoxLayout()
        
        # Left column - Output options
        left_col = QVBoxLayout()
        
        # Output directory selection
        output_layout = QHBoxLayout()
        output_layout.addWidget(QLabel("Output Directory:"))
        
        self.output_dir = None
        self.output_label = QLabel("Same as input files")
        self.output_label.setStyleSheet("font-style: italic;")
        output_layout.addWidget(self.output_label, 1)
        
        self.browse_output_btn = QPushButton("Browse...")
        self.browse_output_btn.clicked.connect(self.browse_output_dir)
        output_layout.addWidget(self.browse_output_btn)
        
        left_col.addLayout(output_layout)
        
        # Output format selection
        format_layout = QHBoxLayout()
        format_layout.addWidget(QLabel("Output Format:"))
        
        self.format_combo = QComboBox()
        self.format_combo.addItems(list(OUTPUT_FORMATS.keys()))
        format_layout.addWidget(self.format_combo)
        
        left_col.addLayout(format_layout)
        
        # Add to options layout
        options_layout.addLayout(left_col)
        
        # Right column - Conversion options
        right_col = QVBoxLayout()
        
        # Overwrite checkbox
        self.overwrite_cb = QCheckBox("Overwrite existing files")
        self.overwrite_cb.setChecked(True)
        right_col.addWidget(self.overwrite_cb)
        
        # Normalize checkbox
        self.normalize_cb = QCheckBox("Normalize audio")
        self.normalize_cb.setChecked(self.config_manager.get("Conversion", "normalize_audio", False))
        right_col.addWidget(self.normalize_cb)
        
        # Remove silence checkbox
        self.remove_silence_cb = QCheckBox("Remove silence")
        self.remove_silence_cb.setChecked(self.config_manager.get("Conversion", "remove_silence", False))
        right_col.addWidget(self.remove_silence_cb)
        
        # Add to options layout
        options_layout.addLayout(right_col)
        
        # Add options layout to main layout
        layout.addLayout(options_layout)
        
        # Add to parent layout if provided, otherwise add to main layout
        if parent_layout:
            parent_layout.addWidget(group_box)
        else:
            self.main_layout.addWidget(group_box)
    
    def list_drag_enter_event(self, event):
        """Custom drag enter event for file list"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def list_drop_event(self, event):
        """Custom drop event for file list"""
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            self.process_dropped_urls(urls)
            event.acceptProposedAction()
    
    def process_dropped_urls(self, urls):
        """Process dropped URLs (files/folders)"""
        files = []
        for url in urls:
            path = url.toLocalFile()
            
            if os.path.isdir(path):
                # Process directory
                self.add_files_from_folder(path)
            elif os.path.isfile(path):
                # Process file
                ext = os.path.splitext(path)[1].lower()
                if ext in ['.nmf', '.g729', '.wav', '.mp3', '.ogg', '.flac']:
                    files.append(path)
        
        if files:
            self.add_files_to_list(files)
    
    def on_file_selection_changed(self):
        """Handle file selection change"""
        selected_items = self.file_list.selectedItems()
        
        if selected_items:
            # Show first selected file in preview
            item = selected_items[0]
            if isinstance(item, CustomListWidgetItem):
                self.preview_panel.set_file(str(item.task.input_path))
        else:
            # Clear preview
            self.preview_panel.clear()
    
    def toggle_log_panel(self):
        """Toggle log panel visibility"""
        log_tab_index = self.bottom_tabs.indexOf(self.log_panel)
        
        if self.show_log_action.isChecked():
            # Show log panel
            if log_tab_index == -1:
                self.bottom_tabs.addTab(self.log_panel, "Log")
        else:
            # Hide log panel
            if log_tab_index != -1:
                self.bottom_tabs.removeTab(log_tab_index)
        
        # Update config
        self.config_manager.set("UI", "show_log_panel", self.show_log_action.isChecked())
    
    def toggle_preview_panel(self):
        """Toggle preview panel visibility"""
        preview_tab_index = self.bottom_tabs.indexOf(self.preview_panel)
        
        if self.show_preview_action.isChecked():
            # Show preview panel
            if preview_tab_index == -1:
                self.bottom_tabs.insertTab(0, self.preview_panel, "Preview")
        else:
            # Hide preview panel
            if preview_tab_index != -1:
                self.bottom_tabs.removeTab(preview_tab_index)
        
        # Update config
        self.config_manager.set("UI", "show_preview_panel", self.show_preview_action.isChecked())
    
    def show_settings(self):
        """Show settings dialog"""
        dialog = SettingsDialog(self.config_manager, self)
        if dialog.exec_() == QDialog.Accepted:
            # Apply settings
            self.apply_font()
            self.apply_theme()
            
            # Update UI based on settings
            if self.show_log_action.isChecked() != self.config_manager.get("UI", "show_log_panel", True):
                self.show_log_action.setChecked(self.config_manager.get("UI", "show_log_panel", True))
                self.toggle_log_panel()
            
            if self.show_preview_action.isChecked() != self.config_manager.get("UI", "show_preview_panel", True):
                self.show_preview_action.setChecked(self.config_manager.get("UI", "show_preview_panel", True))
                self.toggle_preview_panel()
            
            # Update tray icon if needed
            if self.config_manager.get("General", "minimize_to_tray", False) and not hasattr(self, 'tray_icon'):
                self.setup_system_tray()
            
            # Update conversion options
            self.normalize_cb.setChecked(self.config_manager.get("Conversion", "normalize_audio", False))
            self.remove_silence_cb.setChecked(self.config_manager.get("Conversion", "remove_silence", False))
    
    def show_about(self):
        """Show about dialog"""
        dialog = AboutDialog(self)
        dialog.exec_()
    
    def add_files(self):
        """Open file dialog to add files"""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Files", "", 
            "Audio Files (*.nmf *.g729 *.wav *.mp3 *.ogg *.flac);;All Files (*.*)"
        )
        
        if files:
            self.add_files_to_list(files)
    
    def add_folder(self):
        """Open folder dialog to add all audio files from a folder"""
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if folder:
            self.add_files_from_folder(folder)
    
    def add_files_from_folder(self, folder_path):
        """Add all audio files from a folder"""
        path = Path(folder_path)
        extensions = ['.nmf', '.g729', '.wav', '.mp3', '.ogg', '.flac']
        files = []
        
        for ext in extensions:
            files.extend([str(f) for f in path.glob(f'*{ext}')])
        
        if files:
            self.add_files_to_list(files)
            self.statusBar().showMessage(f"Added {len(files)} files from {folder_path}", 5000)
        else:
            QMessageBox.information(
                self, "No Files Found", 
                "No supported audio files found in the selected folder."
            )
    
    def add_files_to_list(self, files):
        """Add files to the list and create conversion tasks"""
        output_format = self.format_combo.currentText()
        
        for file_path in files:
            if not os.path.exists(file_path):
                continue
                
            # Create conversion task
            task = ConversionTask(
                file_path,
                self.output_dir,
                output_format,
                self.overwrite_cb.isChecked(),
                {
                    # Add extra parameters based on UI settings
                    "normalize": "1" if self.normalize_cb.isChecked() else "0",
                    "remove_silence": "1" if self.remove_silence_cb.isChecked() else "0"
                }
            )
            
            # Create list item
            item = CustomListWidgetItem(task)
            self.file_list.addItem(item)
        
        # Update status
        self.update_status()
        
        # Select the first added file if no file is selected
        if not self.file_list.selectedItems() and self.file_list.count() > 0:
            self.file_list.setCurrentRow(0)
    
    def remove_selected(self):
        """Remove selected files from the list"""
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))
        
        # Update status
        self.update_status()
    
    def clear_files(self):
        """Clear all files from the list"""
        self.file_list.clear()
        
        # Update status
        self.update_status()
        
        # Clear preview
        self.preview_panel.clear()
    
    def select_all_files(self):
        """Select all files in the list"""
        for i in range(self.file_list.count()):
            self.file_list.item(i).setSelected(True)
    
    def clear_selection(self):
        """Clear file selection"""
        for i in range(self.file_list.count()):
            self.file_list.item(i).setSelected(False)
    
    def browse_output_dir(self):
        """Select output directory"""
        folder = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if folder:
            self.output_dir = folder
            self.output_label.setText(folder)
            self.output_label.setStyleSheet("")
            
            # Update all tasks with new output directory
            for i in range(self.file_list.count()):
                item = self.file_list.item(i)
                if isinstance(item, CustomListWidgetItem):
                    item.task.output_dir = Path(folder)
                    item.task.output_path = item.task._determine_output_path()
    
    def save_file_list(self):
        """Save file list to a text file"""
        if self.file_list.count() == 0:
            QMessageBox.warning(self, "Warning", "No files to save")
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save File List", "", "Text Files (*.txt);;All Files (*.*)"
        )
        
        if file_path:
            try:
                with open(file_path, 'w') as f:
                    for i in range(self.file_list.count()):
                        item = self.file_list.item(i)
                        if isinstance(item, CustomListWidgetItem):
                            f.write(f"{item.task.input_path}\n")
                
                self.statusBar().showMessage(f"File list saved to {file_path}", 5000)
            except Exception as e:
                logger.error(f"Error saving file list: {e}")
                QMessageBox.warning(self, "Error", f"Could not save file list: {str(e)}")
    
    def load_file_list(self):
        """Load file list from a text file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Load File List", "", "Text Files (*.txt);;All Files (*.*)"
        )
        
        if file_path:
            try:
                with open(file_path, 'r') as f:
                    files = [line.strip() for line in f.readlines() if line.strip()]
                
                valid_files = [f for f in files if os.path.exists(f)]
                
                if valid_files:
                    self.add_files_to_list(valid_files)
                    self.statusBar().showMessage(
                        f"Loaded {len(valid_files)} of {len(files)} files from {file_path}", 
                        5000
                    )
                else:
                    QMessageBox.warning(
                        self, "No Valid Files", 
                        "No valid files found in the file list."
                    )
            except Exception as e:
                logger.error(f"Error loading file list: {e}")
                QMessageBox.warning(self, "Error", f"Could not load file list: {str(e)}")
    
    def update_status(self):
        """Update status bar and UI elements"""
        file_count = self.file_list.count()
        self.status_label.setText(f"Ready - {file_count} files")
        
        # Enable/disable conversion button
        self.convert_btn.setEnabled(file_count > 0)
    
    def start_conversion(self):
        """Start the conversion process"""
        if self.file_list.count() == 0:
            QMessageBox.warning(self, "Warning", "No files selected")
            return
        
        # Create tasks
        tasks = []
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if isinstance(item, CustomListWidgetItem):
                # Update task parameters with current UI settings
                task = item.task
                task.output_format = self.format_combo.currentText()
                task.output_dir = self.output_dir
                task.overwrite = self.overwrite_cb.isChecked()
                task.extra_params = {
                    "normalize": "1" if self.normalize_cb.isChecked() else "0",
                    "remove_silence": "1" if self.remove_silence_cb.isChecked() else "0"
                }
                
                # Reset task status if it was completed/failed
                if task.status in ["completed", "failed"]:
                    task.status = "pending"
                    item.update_display()
                
                tasks.append(task)
        
        # Add tasks to conversion manager
        self.conversion_manager.tasks = tasks
        
        # Start conversion
        self.conversion_manager.start_conversion(
            self.update_progress,
            self.conversion_finished
        )
        
        # Update UI
        self.convert_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.status_label.setText("Converting...")
        
        # Log start
        logger.info(f"Starting conversion of {len(tasks)} files")
    
    def toggle_pause(self):
        """Toggle pause/resume conversion"""
        if self.conversion_manager.paused:
            # Resume
            self.conversion_manager.resume_conversion()
            self.pause_btn.setText("Pause")
            self.pause_btn.setIcon(QApplication.style().standardIcon(QStyle.SP_MediaPause))
            self.status_label.setText("Converting...")
            logger.info("Conversion resumed")
        else:
            # Pause
            self.conversion_manager.pause_conversion()
            self.pause_btn.setText("Resume")
            self.pause_btn.setIcon(QApplication.style().standardIcon(QStyle.SP_MediaPlay))
            self.status_label.setText("Paused")
            logger.info("Conversion paused")
    
    def cancel_conversion(self):
        """Cancel the conversion process"""
        self.conversion_manager.cancel_conversion()
        self.status_label.setText("Canceling...")
        logger.info("Conversion canceled")
    
    def update_progress(self, task, progress):
        """Update progress for a task"""
        # Find the task in the list
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if isinstance(item, CustomListWidgetItem) and item.task == task:
                # Update item display
                item.update_display()
                break
        
        # Update overall progress
        stats = self.conversion_manager.get_stats()
        self.status_progress.setValue(int(stats["progress"] * 100))
        
        # Update status label
        self.status_label.setText(
            f"Converting {stats['completed'] + stats['failed']}/{stats['total']} files"
        )
        
        # Process events to keep UI responsive
        QApplication.processEvents()
    
    def conversion_finished(self, task, success):
        """Handle task completion"""
        # Find the task in the list and update it
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if isinstance(item, CustomListWidgetItem) and item.task == task:
                item.update_display()
                break
        
        # Check if all tasks are finished
        stats = self.conversion_manager.get_stats()
        if stats["pending"] == 0 and stats["processing"] == 0:
            # All tasks finished
            self.status_progress.setValue(100)
            self.status_label.setText(
                f"Conversion complete - {stats['completed']} succeeded, {stats['failed']} failed"
            )
            
            # Reset UI
            self.convert_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
            
            # Log completion
            logger.info(
                f"Conversion complete - {stats['completed']} succeeded, {stats['failed']} failed"
            )
            
            # Show completion message
            QMessageBox.information(
                self, "Conversion Complete", 
                f"Conversion complete\n\n"
                f"Total files: {stats['total']}\n"
                f"Succeeded: {stats['completed']}\n"
                f"Failed: {stats['failed']}"
            )


def main():
    """Main application entry point"""
    # Set up exception handling
    def exception_hook(exc_type, exc_value, exc_traceback):
        logger.critical(
            "Uncaught exception",
            exc_info=(exc_type, exc_value, exc_traceback)
        )
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
    
    sys.excepthook = exception_hook
    
    # Create application
    app = QApplication(sys.argv)
    app.setApplicationName("NMF to WAV Converter")
    app.setApplicationVersion(VERSION)
    app.setStyle("Fusion")  # Modern UI style
    
    # Create and show main window
    window = NMFBatchConverterGUI()
    window.show()
    
    # Run application
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())