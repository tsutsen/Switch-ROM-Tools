import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio
import subprocess
import re
import threading
import sys

# Constants
NSZ_BINARY_PATH = "/app/bin/nsz"
PROD_KEYS_PATH = "~/.switch/prod.keys"
DEFAULT_COMPRESSION_LEVEL = 18
MAX_COMPRESSION_LEVEL = 22
MIN_COMPRESSION_LEVEL = 1
DEFAULT_SCAN_DEPTH = 0
MAX_SCAN_DEPTH = 10
PROCESS_POLL_INTERVAL = 0.1  # seconds
PROCESS_TERMINATE_TIMEOUT = 1  # seconds
READ_BUFFER_SIZE = 4096

class BaseConvertPage(Gtk.Box):
    mode = None                # "compress" or "decompress"
    input_exts = ()
    action_label = "Convert"

    def __init__(self):
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=24
        )

        self.selected_path = None
        self.process = None
        self.total_files = 0
        self.current_file = 0
        self.stopped = False
        self.keys_error = False  # Track if we encounter a keys error

        self._build_ui()

    # ---------------- UI ---------------- #

    def _build_ui(self):
        clamp = Adw.Clamp()
        clamp.set_maximum_size(800)
        clamp.set_margin_top(24)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)

        main_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=24
        )

        # Build UI sections
        self._build_folder_section()
        self._build_advanced_settings()
        self._build_action_buttons()
        self._build_progress_section()

        # Keep output view for internal logging but don't display it
        self.output_view = Gtk.TextView()
        self.output_view.set_editable(False)

        # Assemble
        main_box.append(self.folder_group)
        main_box.append(self.button_box)
        main_box.append(self.progress_group)

        clamp.set_child(main_box)
        self.append(clamp)

    def _build_folder_section(self):
        """Build the folder selection UI section"""
        self.folder_group = Adw.PreferencesGroup()
        self.folder_group.set_title("ROM Folder")
        self.folder_group.set_description(
            "Select the folder containing ROM files"
        )

        self.folder_row = Adw.ActionRow()
        self.folder_row.set_title("No folder selected")
        self.folder_row.set_subtitle("Click to choose a folder")

        icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        self.folder_row.add_prefix(icon)

        self.folder_button = Gtk.Button()
        self.folder_button.set_icon_name("document-open-symbolic")
        self.folder_button.add_css_class("flat")
        self.folder_button.connect("clicked", self.on_folder_select)
        self.folder_row.add_suffix(self.folder_button)

        self.folder_row.set_activatable(True)
        self.folder_row.connect(
            "activated",
            lambda *_: self.folder_button.emit("clicked")
        )

        self.folder_group.add(self.folder_row)

    def _build_advanced_settings(self):
        """Build the advanced settings expander"""
        self.expander = Adw.ExpanderRow()
        self.expander.set_title("Advanced Settings")

        # Delete source switch
        delete_row = Adw.ActionRow()
        delete_row.set_title("Delete source files after completion")
        delete_row.set_subtitle("Use with caution!")

        self.delete_switch = Gtk.Switch()
        self.delete_switch.set_valign(Gtk.Align.CENTER)

        delete_row.add_suffix(self.delete_switch)
        delete_row.set_activatable_widget(self.delete_switch)

        self.expander.add_row(delete_row)

        # Scan depth
        depth_row = Adw.ActionRow()
        depth_row.set_title("Scan depth")
        depth_row.set_subtitle("0 = current folder only, higher values scan subfolders")

        self.scan_depth_spin = Gtk.SpinButton()
        self.scan_depth_spin.set_range(DEFAULT_SCAN_DEPTH, MAX_SCAN_DEPTH)
        self.scan_depth_spin.set_value(DEFAULT_SCAN_DEPTH)
        self.scan_depth_spin.set_increments(1, 1)
        self.scan_depth_spin.set_valign(Gtk.Align.CENTER)

        self.scan_depth_spin.connect(
            "value-changed",
            self.on_scan_depth_changed
        )

        depth_row.add_suffix(self.scan_depth_spin)
        self.expander.add_row(depth_row)

        # Add subclass-specific settings
        self._add_mode_specific_settings()

        self.folder_group.add(self.expander)

    def _build_action_buttons(self):
        """Build the action button section"""
        self.button_box = Gtk.Box(spacing=12)
        self.button_box.set_halign(Gtk.Align.CENTER)

        self.convert_button = Gtk.Button(label=self.action_label)
        self.convert_button.add_css_class("pill")
        self.convert_button.add_css_class("suggested-action")
        self.convert_button.set_sensitive(False)
        self.convert_button.connect("clicked", self.on_convert)

        self.stop_button = Gtk.Button(label="Stop")
        self.stop_button.add_css_class("pill")
        self.stop_button.add_css_class("destructive-action")
        self.stop_button.set_visible(False)
        self.stop_button.connect("clicked", self.on_stop)

        self.button_box.append(self.convert_button)
        self.button_box.append(self.stop_button)

    def _build_progress_section(self):
        """Build the progress indicator section"""
        self.progress_group = Adw.PreferencesGroup()
        self.progress_group.set_title("Progress")
        self.progress_group.set_visible(False)

        self.status_row = Adw.ActionRow()
        self.status_row.set_title("Idle")

        self.status_prefix_box = Gtk.Box()
        self.status_prefix_box.set_size_request(16, 16)

        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)

        self.status_icon = Gtk.Image()
        self.status_icon.set_visible(False)

        self.status_prefix_box.append(self.spinner)
        self.status_prefix_box.append(self.status_icon)

        self.status_row.add_prefix(self.status_prefix_box)

        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_size_request(200, -1)
        self.progress_bar.set_valign(Gtk.Align.CENTER)
        self.status_row.add_suffix(self.progress_bar)

        self.progress_group.add(self.status_row)

    def _process_output_line(self, line, is_carriage_return):
        """
        Process a single line of output from nsz.

        Args:
            line: The output line to process
            is_carriage_return: True if line ended with \r (progress update)
        """
        if not line:
            return

        # Check for keys error
        if "prod.keys" in line.lower() or "keys.txt" in line.lower():
            if "not found" in line.lower():
                self.keys_error = True
                GLib.idle_add(self.append_output, f"❌ ERROR: {line}")
                return

        # Check if it's a progress line
        if "%" in line and any(indicator in line for indicator in ["|", "MiB", "MB", "KB", "B/s"]):
            GLib.idle_add(self.update_progress, line)
        elif not is_carriage_return:
            # Only log non-progress lines that end with newline
            GLib.idle_add(self.append_output, line)

    def _terminate_process(self):
        """Gracefully terminate the current process, with fallback to kill."""
        if not self.process:
            return

        try:
            self.process.terminate()
        except Exception:
            pass

        # Give it a moment to terminate gracefully
        try:
            self.process.wait(timeout=PROCESS_TERMINATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                self.process.kill()
                self.process.wait()
            except Exception:
                pass

    def _add_mode_specific_settings(self):
        """Override in subclasses to add mode-specific settings"""
        pass

    def on_scan_depth_changed(self, spin_button):
        if not self.selected_path:
            return

        depth = int(spin_button.get_value())
        count = self.count_input_files(self.selected_path, depth)

        if count > 0:
            self.folder_row.set_subtitle(
                f"Found {count} file{'s' if count != 1 else ''} ready to process"
            )
            self.convert_button.set_sensitive(True)
        else:
            self.folder_row.set_subtitle("No compatible files found")
            self.convert_button.set_sensitive(False)

    # ---------------- Logic ---------------- #

    def on_folder_select(self, *_):
        dialog = Gtk.FileDialog()
        dialog.set_title("Select ROM Folder")
        dialog.select_folder(None, None, self.on_folder_selected)

    def on_folder_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            if not folder:
                return

            self.selected_path = folder.get_path()
            depth = int(self.scan_depth_spin.get_value())

            count = self.count_input_files(self.selected_path, depth)
            self.folder_row.set_title(self.selected_path)

            if count > 0:
                self.folder_row.set_subtitle(
                    f"Found {count} file{'s' if count != 1 else ''}"
                )
                self.convert_button.set_sensitive(True)
            else:
                self.folder_row.set_subtitle("No compatible files found")
                self.convert_button.set_sensitive(False)

        except Exception:
            pass

    # ---------- File discovery ---------- #

    def get_input_files(self, path, max_depth):
        """
        Recursively find all compatible ROM files in a directory.

        Args:
            path: Root directory to scan
            max_depth: Maximum recursion depth (0 = current dir only)

        Returns:
            List of absolute file paths
        """
        import os

        files = []

        def scan(current_path, depth):
            try:
                for name in os.listdir(current_path):
                    full = os.path.join(current_path, name)

                    if os.path.isfile(full):
                        if full.lower().endswith(self.input_exts):
                            files.append(full)

                    elif os.path.isdir(full):
                        # Don't recurse if max_depth is 0 or we've reached max depth
                        if max_depth == 0:
                            continue
                        if depth >= max_depth:
                            continue

                        scan(full, depth + 1)
            except Exception:
                pass

        scan(path, 0)
        return files

    def count_input_files(self, path, max_depth):
        """Count compatible files in directory."""
        return len(self.get_input_files(path, max_depth))

    # ---------- Conversion ---------- #

    def on_convert(self, *_):
        if not self.selected_path:
            return

        self.stopped = False  # Reset stop flag
        self.keys_error = False  # Reset keys error flag

        self.status_icon.set_visible(False)
        self.status_icon.remove_css_class("success")
        self.status_icon.remove_css_class("error")

        depth = int(self.scan_depth_spin.get_value())
        self.total_files = self.count_input_files(self.selected_path, depth)
        self.current_file = 0

        buffer = self.output_view.get_buffer()
        buffer.set_text("")

        self.convert_button.set_visible(False)
        self.stop_button.set_visible(True)
        self.folder_button.set_sensitive(False)
        self.progress_group.set_visible(True)
        self.progress_bar.set_fraction(0.0)

        self.spinner.set_visible(True)
        self.spinner.start()

        self.status_icon.set_visible(False)

        self.status_row.set_title("Processing")
        self.status_row.set_subtitle("Starting...")

        thread = threading.Thread(target=self.run_conversion, daemon=True)
        thread.start()

    def on_stop(self, *_):
        """Stop the current conversion process"""
        if not self.stopped:
            self.stopped = True
            GLib.idle_add(self.append_output, "\n⚠️ Stopping process...")
            self.stop_button.set_sensitive(False)  # Disable to prevent multiple clicks

    def update_progress(self, progress_text):
        # Extract percentage
        match = re.search(r'(\d+)%', progress_text)
        if not match:
            return

        percentage = int(match.group(1)) / 100.0
        self.progress_bar.set_fraction(percentage)

        # Try to extract speed and time info - format: [00:02<00:02, 253.83 MiB/s]
        info_match = re.search(
            r'\[[\d:]+<([\d:]+),\s*([\d.]+\s*\w+/s)\]',
            progress_text
        )

        if info_match:
            remaining, speed = info_match.groups()
            self.status_row.set_subtitle(
                f"{int(percentage * 100)}% • {speed} • {remaining} remaining"
            )
        else:
            # Just show the percentage
            self.status_row.set_subtitle(f"{int(percentage * 100)}%")

    def update_file_count(self):
        if self.total_files > 1:
            self.status_row.set_title(
                f"Processing ({self.current_file}/{self.total_files})"
            )
        else:
            self.status_row.set_title("Processing")
        return False

    def build_command(self, file_path):
        """Build the nsz command for a single file. Override in subclasses."""
        cmd = [NSZ_BINARY_PATH]
        cmd.append("-D" if self.mode == "decompress" else "-C")

        if self.delete_switch.get_active():
            cmd.extend(["-V", "--rm-source"])

        cmd.append(file_path)
        return cmd

    def run_conversion(self):
        """Main conversion loop that processes all files."""
        import os
        import pty

        try:
            depth = int(self.scan_depth_spin.get_value())
            files = self.get_input_files(self.selected_path, depth)

            if not files:
                GLib.idle_add(self.append_output, "No files found")
                GLib.idle_add(self.on_complete, False, False)
                return

            self.total_files = len(files)
            successful = 0

            for idx, path in enumerate(files, 1):
                # Check if stopped between files
                if self.stopped:
                    GLib.idle_add(self.append_output, "\n⚠️ Process stopped by user")
                    GLib.idle_add(self.on_complete, False, True)
                    return

                self.current_file = idx
                GLib.idle_add(self.update_file_count)

                GLib.idle_add(
                    self.append_output,
                    f"\n--- Processing {idx}/{self.total_files}: {os.path.basename(path)} ---"
                )

                # Build and run command
                cmd = self.build_command(path)
                master, slave = pty.openpty()

                self.process = subprocess.Popen(
                    cmd,
                    stdout=slave,
                    stderr=slave,
                    close_fds=True
                )
                os.close(slave)

                # Read output with non-blocking I/O
                was_stopped = self._read_process_output_with_select(master)

                if was_stopped:
                    GLib.idle_add(self.on_complete, False, True)
                    return

                # Check if we hit a keys error
                if self.keys_error:
                    GLib.idle_add(
                        self.append_output,
                        "\n❌ Invalid or missing prod.keys file!\n"
                        "Please restart the app and provide a valid keys file."
                    )
                    GLib.idle_add(self.on_complete, False, False)
                    # Ask user to reload
                    GLib.idle_add(self.show_keys_error_dialog)
                    return

                # Check result
                if self.process.returncode == 0:
                    successful += 1
                    GLib.idle_add(
                        self.append_output,
                        f"✓ Successfully processed {os.path.basename(path)}"
                    )
                else:
                    GLib.idle_add(
                        self.append_output,
                        f"✗ Failed to process {os.path.basename(path)}"
                    )

            GLib.idle_add(self.on_complete, successful == self.total_files, False)

        except Exception as e:
            GLib.idle_add(self.append_output, f"Error: {e}")
            GLib.idle_add(self.on_complete, False, False)

    def _read_process_output_with_select(self, master):
        """
        Read output from process using select() for non-blocking I/O.

        Args:
            master: PTY master file descriptor

        Returns:
            bool: True if stopped by user, False otherwise
        """
        import os
        import fcntl
        import select

        # Set non-blocking mode
        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        output_buffer = b""

        while True:
            # Check if stopped
            if self.stopped:
                self._terminate_process()
                try:
                    os.close(master)
                except Exception:
                    pass
                return True  # Stopped by user

            # Check if process is still running
            if self.process.poll() is not None:
                # Process ended, read any remaining output
                try:
                    remaining = os.read(master, READ_BUFFER_SIZE)
                    if remaining:
                        output_buffer += remaining
                except Exception:
                    pass
                break

            # Try to read output
            try:
                ready, _, _ = select.select([master], [], [], PROCESS_POLL_INTERVAL)
                if ready:
                    chunk = os.read(master, READ_BUFFER_SIZE)
                    if chunk:
                        output_buffer += chunk

                        # Process lines - split by both \n and \r
                        while b'\n' in output_buffer or b'\r' in output_buffer:
                            newline_pos = output_buffer.find(b'\n')
                            carriage_pos = output_buffer.find(b'\r')

                            if newline_pos == -1:
                                split_pos = carriage_pos
                                is_carriage = True
                            elif carriage_pos == -1:
                                split_pos = newline_pos
                                is_carriage = False
                            else:
                                split_pos = min(newline_pos, carriage_pos)
                                is_carriage = (carriage_pos < newline_pos)

                            if split_pos == -1:
                                break

                            line_bytes = output_buffer[:split_pos]
                            output_buffer = output_buffer[split_pos + 1:]

                            try:
                                line = line_bytes.decode('utf-8', errors='replace').strip()
                                self._process_output_line(line, is_carriage)
                            except Exception:
                                pass
            except (OSError, IOError):
                pass

        # Process any remaining output
        if output_buffer:
            try:
                line = output_buffer.decode('utf-8', errors='replace').rstrip("\r\n")
                if line:
                    GLib.idle_add(self.append_output, line)
            except Exception:
                pass

        try:
            os.close(master)
        except Exception:
            pass

        return False  # Not stopped by user

    def append_output(self, text):
        buf = self.output_view.get_buffer()
        buf.insert(buf.get_end_iter(), text + "\n")

        # Auto-scroll to bottom
        mark = buf.get_insert()
        self.output_view.scroll_to_mark(mark, 0.0, False, 0.0, 0.0)

    def show_keys_error_dialog(self):
        """Show dialog informing user about invalid keys and offer to reload."""
        window = self.get_root()
        if isinstance(window, SwitchROMToolsWindow):
            # Rebuild the prod.keys UI
            window.build_prod_keys_ui()
            window.show_toast("Invalid prod.keys detected! Please select a valid file.")

    def on_complete(self, success, stopped=False):
        self.spinner.stop()
        self.spinner.set_visible(False)

        self.status_icon.set_visible(True)

        self.convert_button.set_visible(True)
        self.stop_button.set_visible(False)
        self.stop_button.set_sensitive(True)  # Re-enable for next time
        self.convert_button.set_sensitive(True)
        self.folder_button.set_sensitive(True)

        if stopped:
            self.status_row.set_title("Stopped")
            self.status_row.set_subtitle("Process interrupted by user")

            self.status_icon.set_from_icon_name("process-stop-symbolic")
            self.status_icon.add_css_class("error")
        elif success:
            self.status_row.set_title("Completed")
            self.status_row.set_subtitle(
                f"Successfully processed {self.total_files} file"
                f"{'s' if self.total_files != 1 else ''}"
            )
            self.progress_bar.set_fraction(1.0)

            self.status_icon.set_from_icon_name("object-select-symbolic")
            self.status_icon.add_css_class("success")
        else:
            self.status_row.set_title("Failed")
            self.status_row.set_subtitle("Check output log for details")

            self.status_icon.set_from_icon_name("dialog-error-symbolic")
            self.status_icon.add_css_class("error")


class DecompressPage(BaseConvertPage):
    mode = "decompress"
    input_exts = (".nsz", ".xcz", ".ncz")
    action_label = "Decompress to NSP/XCI"

    def build_command(self, file_path):
        """Build the nsz decompression command"""
        cmd = [NSZ_BINARY_PATH, "-D"]

        # Add verification if enabled
        if hasattr(self, 'verify_switch') and self.verify_switch.get_active():
            cmd.append("-V")

        # Delete source if checked
        if self.delete_switch.get_active():
            cmd.extend(["-V", "--rm-source"])

        cmd.append(file_path)
        return cmd

    def _add_mode_specific_settings(self):
        """Add decompression-specific settings"""
        # Verification switch
        verify_row = Adw.ActionRow()
        verify_row.set_title("Verify after decompression")
        verify_row.set_subtitle("Verify file integrity after decompression")

        self.verify_switch = Gtk.Switch()
        self.verify_switch.set_valign(Gtk.Align.CENTER)
        self.verify_switch.set_active(True)  # Enable by default

        verify_row.add_suffix(self.verify_switch)
        verify_row.set_activatable_widget(self.verify_switch)

        self.expander.add_row(verify_row)


class CompressPage(BaseConvertPage):
    mode = "compress"
    input_exts = (".nsp", ".xci")
    action_label = "Compress to NSZ/XCZ"

    def _add_mode_specific_settings(self):
        """Add compression-specific settings"""

        # Compression level
        level_row = Adw.ActionRow()
        level_row.set_title("Compression level")
        level_row.set_subtitle("Balanced speed and size (default: 18)")

        self.level_spin = Gtk.SpinButton()
        self.level_spin.set_range(MIN_COMPRESSION_LEVEL, MAX_COMPRESSION_LEVEL)
        self.level_spin.set_increments(1, 1)
        self.level_spin.set_value(DEFAULT_COMPRESSION_LEVEL)
        self.level_spin.set_valign(Gtk.Align.CENTER)

        level_row.add_suffix(self.level_spin)
        self.expander.add_row(level_row)

        # Dynamic subtitle based on level
        def on_level_changed(spin):
            val = int(spin.get_value())
            if val <= 10:
                level_row.set_subtitle("Fast compression, larger files")
            elif val <= 18:
                level_row.set_subtitle("Balanced speed and size")
            else:
                level_row.set_subtitle("Maximum compression, slower")

        self.level_spin.connect("value-changed", on_level_changed)

        # Compression mode
        mode_row = Adw.ActionRow()
        mode_row.set_title("Compression mode")
        mode_row.set_subtitle("Solid = better compression, Block = random access")

        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        mode_box.set_valign(Gtk.Align.CENTER)

        self.solid_button = Gtk.ToggleButton(label="Solid")
        self.solid_button.set_active(True)
        self.solid_button.add_css_class("flat")

        self.block_button = Gtk.ToggleButton(label="Block")
        self.block_button.add_css_class("flat")

        # Link the buttons so only one can be active
        self.solid_button.connect("toggled", lambda btn:
            self.block_button.set_active(not btn.get_active()) if btn.get_active() else None
        )
        self.block_button.connect("toggled", lambda btn:
            self.solid_button.set_active(not btn.get_active()) if btn.get_active() else None
        )

        mode_box.append(self.solid_button)
        mode_box.append(self.block_button)

        mode_row.add_suffix(mode_box)
        self.expander.add_row(mode_row)

        # Verification
        verify_row = Adw.ActionRow()
        verify_row.set_title("Verify after compression")
        verify_row.set_subtitle("Recommended when using delete source files")

        self.verify_switch = Gtk.Switch()
        self.verify_switch.set_valign(Gtk.Align.CENTER)
        self.verify_switch.set_active(True)

        verify_row.add_suffix(self.verify_switch)
        verify_row.set_activatable_widget(self.verify_switch)

        self.expander.add_row(verify_row)

        # Multi-threading
        threads_row = Adw.ActionRow()
        threads_row.set_title("Threads")
        threads_row.set_subtitle("0 = auto-detect CPU cores")

        self.threads_spin = Gtk.SpinButton()
        self.threads_spin.set_range(0, 32)
        self.threads_spin.set_value(0)
        self.threads_spin.set_increments(1, 1)
        self.threads_spin.set_valign(Gtk.Align.CENTER)

        threads_row.add_suffix(self.threads_spin)
        self.expander.add_row(threads_row)

    def build_command(self, file_path):
        """Build the nsz compression command"""
        cmd = [NSZ_BINARY_PATH, "-C"]

        # Compression level
        level = int(self.level_spin.get_value())
        cmd.extend(["-l", str(level)])

        # Compression mode
        if self.solid_button.get_active():
            cmd.append("-S")
        else:
            cmd.append("-B")

        # Threading
        threads = int(self.threads_spin.get_value())
        if threads > 0:
            cmd.extend(["-t", str(threads)])

        # Verification
        if self.verify_switch.get_active():
            cmd.append("-V")

        # Delete source if checked (requires verification)
        if self.delete_switch.get_active():
            if not self.verify_switch.get_active():
                cmd.append("-V")  # Force verification when deleting source
            cmd.append("--rm-source")

        cmd.append(file_path)
        return cmd


class SwitchROMToolsWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.set_title("Switch ROM Tools")
        self.set_default_size(700, 700)
        # Allow resizing but set minimum size
        self.set_size_request(600, 350)

        # Check prod.keys first
        if not self.check_prod_keys():
            self.build_prod_keys_ui()
        else:
            self.build_ui()

    # ---------- prod.keys handling ---------- #

    def check_prod_keys(self):
        import os
        return os.path.exists(
            os.path.expanduser(PROD_KEYS_PATH)
        )

    def build_prod_keys_ui(self):
        self.toast_overlay = Adw.ToastOverlay()

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        status = Adw.StatusPage()
        status.set_icon_name("dialog-warning-symbolic")
        status.set_title("Keys Required!")
        status.set_description(
            "The prod.keys file is required to use this app.\n"
            "This file contains encryption keys from your Nintendo Switch.\n\n"
            "Please dump your keys using Lockpick_RCM.\n"

            "Select your prod.keys file to continue."
        )

        button = Gtk.Button(label="Select prod.keys")
        button.add_css_class("pill")
        button.add_css_class("suggested-action")
        button.connect("clicked", self.select_prod_keys_file)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12
        )
        box.set_halign(Gtk.Align.CENTER)
        box.append(button)

        status.set_child(box)

        toolbar_view.set_content(status)
        self.toast_overlay.set_child(toolbar_view)
        self.set_content(self.toast_overlay)

    def select_prod_keys_file(self, *_):
        dialog = Gtk.FileDialog()
        dialog.set_title("Select prod.keys")
        dialog.open(self, None, self.on_prod_keys_selected)

    def on_prod_keys_selected(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if not file:
                return

            import os, shutil
            src = file.get_path()
            dst_dir = os.path.expanduser("~/.switch")
            os.makedirs(dst_dir, exist_ok=True)
            dst_path = os.path.join(dst_dir, "prod.keys")

            # Copy the file
            shutil.copy2(src, dst_path)

            # Basic validation - just check if file is not empty
            if os.path.getsize(dst_path) > 0:
                self.build_ui()
                self.show_toast("prod.keys installed successfully!")
            else:
                # Empty file
                try:
                    os.remove(dst_path)
                except Exception:
                    pass
                self.show_toast("Invalid prod.keys file - file is empty!")

        except Exception as e:
            self.show_toast(f"Failed to install prod.keys: {e}")

    # ---------- main UI ---------- #

    def build_ui(self):
        self.toast_overlay = Adw.ToastOverlay()

        toolbar_view = Adw.ToolbarView()

        # Header with ViewSwitcher
        header = Adw.HeaderBar()

        # Add app icon to header (shown when not in overview mode)
        # Note: In a real Flatpak, the icon should be installed to the system
        # For development, we can show it this way

        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.view_stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)

        header.set_title_widget(switcher)
        toolbar_view.add_top_bar(header)

        # Pages
        decompress_page = self.view_stack.add_titled(
            DecompressPage(),
            "decompress",
            "Decompress"
        )
        decompress_page.set_icon_name("view-fullscreen-symbolic")

        compress_page = self.view_stack.add_titled(
            CompressPage(),
            "compress",
            "Compress"
        )
        compress_page.set_icon_name("view-restore-symbolic")

        toolbar_view.set_content(self.view_stack)
        self.toast_overlay.set_child(toolbar_view)
        self.set_content(self.toast_overlay)

    # ---------- toast ---------- #

    def show_toast(self, message):
        toast = Adw.Toast.new(message)
        toast.set_timeout(3)
        self.toast_overlay.add_toast(toast)


class SwitchROMToolsApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id='org.tsutsen.SwitchROMTools',
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )

        # Set app icon
        Gtk.Window.set_default_icon_name('org.tsutsen.SwitchROMTools')

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = SwitchROMToolsWindow(application=self)
            win.set_icon_name('org.tsutsen.SwitchROMTools')
        win.present()


def main(version):
    """Main entry point"""
    app = SwitchROMToolsApp()
    return app.run(sys.argv)


if __name__ == '__main__':
    app = SwitchROMToolsApp()
    app.run(sys.argv)
