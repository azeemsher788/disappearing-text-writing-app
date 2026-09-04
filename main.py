"""
The Most Dangerous Writing App — Python Edition
================================================
A minimalist, distraction-free writing application that permanently deletes
all progress if the user stops typing for a configurable number of seconds.

Architecture:
    - AppState        : Manages the session lifecycle and countdown state (Model).
    - TimerController : Encapsulates all `after()`/`after_cancel()` debounce
                        logic, keeping the Tk event loop clean (Controller).
    - WritingApp      : Owns the customtkinter root window and all widgets (View).
    - main()          : Entry point — wires View ↔ Controller ↔ Model.

Bug fixes vs v1:
    - Overlay `tk.Frame` no longer steals focus from the text widget.
      Keystrokes are now bound on both the overlay AND the text widget so
      the first character typed always transitions IDLE -> ACTIVE correctly.
    - `self._text.focus_set()` is called on startup and after every erase
      so the cursor is immediately ready without requiring a mouse click.

Author : Azeem Sher
License: MIT
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional

import customtkinter as ctk


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IDLE_TIMEOUT_MS: int = 5_000          # milliseconds before all text is erased
TICK_INTERVAL_MS: int = 50            # how often the progress bar/feedback updates
FONT_FAMILY: str = "Georgia"
FONT_SIZE: int = 22                   # comfortable reading/writing size
STATUS_FONT: str = "Inter"
WINDOW_TITLE: str = "The Most Dangerous Writing App"
WINDOW_MIN_W: int = 860
WINDOW_MIN_H: int = 620
APP_THEME: str = "dark"

# Colour palette
COLOUR_BG: str = "#0D1117"            # deep GitHub-dark background
COLOUR_SAFE: str = "#E8E6E1"          # warm off-white — text in safe state
COLOUR_WARNING: str = "#F59E0B"       # amber  (<=50 % time remaining)
COLOUR_DANGER: str = "#EF4444"        # red    (<=20 % time remaining)
COLOUR_ERASED: str = "#2D3748"        # dimmed — shown momentarily on erase
COLOUR_STATUS_BG: str = "#161B22"     # status bar background
COLOUR_BAR_BG: str = "#1C2333"        # progress bar track background
COLOUR_BAR_SAFE: str = "#10B981"      # green fill — plenty of time
COLOUR_BAR_WARNING: str = "#F59E0B"   # amber fill — getting close
COLOUR_BAR_DANGER: str = "#EF4444"    # red fill   — about to erase
BAR_HEIGHT: int = 8                   # px — tall enough to be noticed instantly


# ---------------------------------------------------------------------------
# Model — Application State
# ---------------------------------------------------------------------------

class SessionPhase(Enum):
    """Represents the distinct phases a writing session moves through."""
    IDLE = auto()        # App just opened, waiting for the first keystroke
    ACTIVE = auto()      # User is typing; the dangerous timer is armed
    ERASED = auto()      # Text was deleted; brief flash state before reset


@dataclass
class AppState:
    """
    Pure-data model for a single writing session.

    Attributes:
        phase:           Current lifecycle phase of the session.
        word_count:      Number of whitespace-separated words in the buffer.
        char_count:      Total characters written.
        progress_ratio:  Float in [0.0, 1.0] representing remaining idle time.
                         1.0 = full time remaining, 0.0 = about to erase.
        session_best:    Highest word count reached in this app lifecycle.
    """
    phase: SessionPhase = SessionPhase.IDLE
    word_count: int = 0
    char_count: int = 0
    progress_ratio: float = 1.0
    session_best: int = 0
    _elapsed_ms: int = field(default=0, repr=False)

    def reset(self) -> None:
        """Reset mutable per-session fields, preserving ``session_best``."""
        self.phase = SessionPhase.IDLE
        self.word_count = 0
        self.char_count = 0
        self.progress_ratio = 1.0
        self._elapsed_ms = 0

    def update_counts(self, text: str) -> None:
        """Recompute word/char counts from the current text buffer."""
        self.char_count = len(text)
        self.word_count = len(text.split()) if text.strip() else 0
        if self.word_count > self.session_best:
            self.session_best = self.word_count

    def tick(self, delta_ms: int) -> None:
        """
        Advance the elapsed idle counter by *delta_ms* milliseconds and
        recompute :attr:`progress_ratio`.
        """
        self._elapsed_ms += delta_ms
        self.progress_ratio = max(
            0.0, 1.0 - (self._elapsed_ms / IDLE_TIMEOUT_MS)
        )

    def reset_idle_clock(self) -> None:
        """Called on every keystroke to restart the idle countdown."""
        self._elapsed_ms = 0
        self.progress_ratio = 1.0


# ---------------------------------------------------------------------------
# Controller — Timer / Debounce Logic
# ---------------------------------------------------------------------------

class TimerController:
    """
    Manages the Tk ``after()`` / ``after_cancel()`` debounce machinery.

    By funnelling all timer scheduling through a single controller we guarantee:
    - At most **one** pending idle-erase callback at any moment.
    - At most **one** pending progress-bar tick callback at any moment.
    - Clean cancellation without dangling handles that could crash the app.

    Args:
        root:      The root ``ctk.CTk`` window (used to schedule callbacks).
        state:     Shared :class:`AppState` instance.
        on_erase:  Callable invoked when idle time expires; receives no args.
        on_tick:   Callable invoked every :data:`TICK_INTERVAL_MS` ms while
                   the session is active; used to update visual feedback.
    """

    def __init__(
        self,
        root: ctk.CTk,
        state: AppState,
        on_erase: Callable[[], None],
        on_tick: Callable[[], None],
    ) -> None:
        self._root = root
        self._state = state
        self._on_erase = on_erase
        self._on_tick = on_tick
        self._erase_handle: Optional[str] = None
        self._tick_handle: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def arm(self) -> None:
        """
        Arm (or re-arm) the danger timer.

        Safe to call on every ``<KeyRelease>`` event:
        - Cancels any pending erase callback (debounce).
        - Resets the idle clock in the state model.
        - Schedules a fresh erase callback for :data:`IDLE_TIMEOUT_MS` ms.
        - Ensures the progress-bar tick loop is running.
        """
        self._state.reset_idle_clock()
        self._cancel_erase()
        self._erase_handle = self._root.after(IDLE_TIMEOUT_MS, self._fire_erase)
        if self._tick_handle is None:
            self._schedule_tick()

    def disarm(self) -> None:
        """Cancel all pending callbacks — called on erase or session reset."""
        self._cancel_erase()
        self._cancel_tick()
        self._state.progress_ratio = 1.0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _cancel_erase(self) -> None:
        if self._erase_handle is not None:
            self._root.after_cancel(self._erase_handle)
            self._erase_handle = None

    def _cancel_tick(self) -> None:
        if self._tick_handle is not None:
            self._root.after_cancel(self._tick_handle)
            self._tick_handle = None

    def _fire_erase(self) -> None:
        """Callback executed when idle time expires."""
        self._erase_handle = None
        self._cancel_tick()
        self._on_erase()

    def _schedule_tick(self) -> None:
        self._tick_handle = self._root.after(TICK_INTERVAL_MS, self._do_tick)

    def _do_tick(self) -> None:
        """Advance the idle counter and request a UI repaint."""
        self._tick_handle = None
        self._state.tick(TICK_INTERVAL_MS)
        self._on_tick()
        if self._state.phase == SessionPhase.ACTIVE:
            self._schedule_tick()


# ---------------------------------------------------------------------------
# View — customtkinter UI
# ---------------------------------------------------------------------------

class WritingApp:
    """
    The primary View class.  Owns the ``ctk.CTk`` root window and all widgets.
    Binds user events and delegates state transitions to :class:`TimerController`.

    Layout (top to bottom):
        Row 0 — Danger Progress Bar  (BAR_HEIGHT px strip, always visible)
        Row 1 — Status Bar           (hint text | countdown timer | word/char count)
        Row 2 — Text Area + Overlay  (fills all remaining vertical space)

    Focus contract:
        The text widget always holds keyboard focus.  The idle overlay is a
        visual layer placed *above* the text widget via ``place()``, but
        keystrokes are forwarded from the overlay back to the text widget so
        the first character the user types is never lost.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        ctk.set_appearance_mode(APP_THEME)
        ctk.set_default_color_theme("dark-blue")

        self._root = ctk.CTk()
        self._root.title(WINDOW_TITLE)
        self._root.geometry("1080x720")
        self._root.minsize(WINDOW_MIN_W, WINDOW_MIN_H)
        self._root.configure(fg_color=COLOUR_BG)

        self._state = AppState()
        self._timer = TimerController(
            root=self._root,
            state=self._state,
            on_erase=self._handle_erase,
            on_tick=self._refresh_feedback,
        )

        self._build_ui()
        self._bind_events()
        self._show_idle_overlay()

        # Give focus to the text widget immediately so the user can start
        # typing without having to click first.
        self._root.after(100, self._text.focus_set)

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Construct and grid every widget in the window."""
        self._root.grid_rowconfigure(2, weight=1)
        self._root.grid_columnconfigure(0, weight=1)

        # ── 1. Danger Progress Bar ─────────────────────────────────────
        self._bar_canvas = tk.Canvas(
            self._root,
            height=BAR_HEIGHT,
            bg=COLOUR_BAR_BG,
            highlightthickness=0,
            bd=0,
        )
        self._bar_canvas.grid(row=0, column=0, sticky="ew")
        self._bar_fill = self._bar_canvas.create_rectangle(
            0, 0, 0, BAR_HEIGHT, fill=COLOUR_BAR_SAFE, outline=""
        )

        # ── 2. Status Bar ──────────────────────────────────────────────
        status_frame = ctk.CTkFrame(
            self._root,
            fg_color=COLOUR_STATUS_BG,
            corner_radius=0,
            height=44,
        )
        status_frame.grid(row=1, column=0, sticky="ew")
        status_frame.grid_propagate(False)
        status_frame.grid_columnconfigure(1, weight=1)

        self._lbl_session = ctk.CTkLabel(
            status_frame,
            text="Start typing — stop for 5 seconds and everything is gone.",
            font=(STATUS_FONT, 13),
            text_color="#8B949E",
            anchor="w",
        )
        self._lbl_session.grid(row=0, column=0, padx=20, pady=0, sticky="w")

        self._lbl_timer = ctk.CTkLabel(
            status_frame,
            text="",
            font=(STATUS_FONT, 13, "bold"),
            text_color=COLOUR_SAFE,
            anchor="center",
        )
        self._lbl_timer.grid(row=0, column=1, padx=8, sticky="ew")

        self._lbl_counts = ctk.CTkLabel(
            status_frame,
            text="0 words  ·  0 chars",
            font=(STATUS_FONT, 13, "bold"),
            text_color="#484F58",
            anchor="e",
        )
        self._lbl_counts.grid(row=0, column=2, padx=20, pady=0, sticky="e")

        # ── 3. Text Frame ──────────────────────────────────────────────
        self._text_frame = ctk.CTkFrame(
            self._root, fg_color=COLOUR_BG, corner_radius=0
        )
        self._text_frame.grid(row=2, column=0, sticky="nsew")
        self._text_frame.grid_rowconfigure(0, weight=1)
        self._text_frame.grid_columnconfigure(0, weight=1)

        self._text = tk.Text(
            self._text_frame,
            font=(FONT_FAMILY, FONT_SIZE),
            bg=COLOUR_BG,
            fg=COLOUR_SAFE,
            insertbackground=COLOUR_SAFE,
            insertwidth=3,
            relief="flat",
            bd=0,
            wrap="word",
            padx=100,
            pady=70,
            undo=True,
            spacing1=6,
            spacing3=6,
            highlightthickness=0,
            selectbackground="#30363D",
            selectforeground=COLOUR_SAFE,
        )
        self._text.grid(row=0, column=0, sticky="nsew")

        # Subtle scrollbar
        self._scrollbar = ctk.CTkScrollbar(
            self._text_frame,
            command=self._text.yview,
            fg_color=COLOUR_BG,
            button_color="#30363D",
            button_hover_color="#484F58",
        )
        self._scrollbar.grid(row=0, column=1, sticky="ns")
        self._text.configure(yscrollcommand=self._scrollbar.set)

        # ── 4. Idle / Erase Overlay ────────────────────────────────────
        # Placed *inside* text_frame using place() so it floats above the
        # text widget.  Key events are forwarded to self._text so no
        # keystroke is ever lost when the overlay is showing.
        self._overlay = tk.Frame(self._text_frame, bg=COLOUR_BG)

        # Title label
        self._overlay_title = tk.Label(
            self._overlay,
            text="",
            font=("Inter", 15, "bold"),
            bg=COLOUR_BG,
            fg="#30363D",
            justify="center",
        )
        self._overlay_title.pack(pady=(0, 20))

        # Body label
        self._overlay_body = tk.Label(
            self._overlay,
            text="",
            font=("Georgia", 20),
            bg=COLOUR_BG,
            fg="#484F58",
            wraplength=520,
            justify="center",
        )
        self._overlay_body.pack()

        # Centre the overlay group
        self._overlay_label = tk.Label(
            self._overlay,
            text="",
            font=("Inter", 26, "bold"),
            bg=COLOUR_BG,
            fg="#8B949E",
            wraplength=560,
            justify="center",
        )
        self._overlay_label.place(relx=0.5, rely=0.5, anchor="center")

    # ------------------------------------------------------------------
    # Event Binding
    # ------------------------------------------------------------------

    def _bind_events(self) -> None:
        """Attach all event handlers."""
        # Primary typing target
        self._text.bind("<KeyRelease>", self._on_key_release)

        # Forward keystrokes from overlay to text widget so the user can
        # start typing without clicking — the overlay never "eats" input.
        self._overlay.bind("<Key>", self._forward_key_to_text)
        self._overlay_label.bind("<Key>", self._forward_key_to_text)

        # Click anywhere on the overlay focuses the hidden text widget
        self._overlay.bind("<Button-1>", lambda _e: self._text.focus_set())
        self._overlay_label.bind("<Button-1>", lambda _e: self._text.focus_set())

        self._root.bind("<Configure>", self._on_resize)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Event Handlers
    # ------------------------------------------------------------------

    def _forward_key_to_text(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """
        Redirect a keystroke that landed on the overlay into the text widget.

        This keeps focus on ``self._text`` so the very first character the
        user types is not swallowed by the overlay frame.
        """
        self._text.focus_set()
        # Synthesise the event on the text widget
        if event.char:
            self._text.insert("insert", event.char)
            self._text.event_generate("<KeyRelease>", keysym=event.keysym)

    def _on_key_release(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """
        Fired after every key is released inside the text area.

        Flow:
        1. Ignore pure modifier keys (Shift, Ctrl, Alt ...).
        2. Transition IDLE -> ACTIVE on the first meaningful keystroke.
        3. Re-arm the danger timer (debounce — cancels + reschedules).
        4. Update word/char counts in the state model.
        5. Refresh all visual feedback widgets.
        """
        if event.keysym in {
            "Shift_L", "Shift_R", "Control_L", "Control_R",
            "Alt_L", "Alt_R", "Meta_L", "Meta_R",
            "Caps_Lock", "Num_Lock", "Scroll_Lock",
            "Super_L", "Super_R",
        }:
            return

        raw_text = self._text.get("1.0", "end-1c")

        # First real keystroke: start the session
        if self._state.phase == SessionPhase.IDLE and raw_text.strip():
            self._state.phase = SessionPhase.ACTIVE
            self._hide_overlay()

        if self._state.phase == SessionPhase.ACTIVE:
            self._timer.arm()
            self._state.update_counts(raw_text)
            self._refresh_feedback()

    def _handle_erase(self) -> None:
        """
        Callback invoked by :class:`TimerController` when idle time expires.

        Sequence:
        1. Transition state -> ERASED.
        2. Play the flash animation.
        3. Dim the text colour.
        4. Schedule the actual wipe after the animation completes.
        """
        self._state.phase = SessionPhase.ERASED
        self._flash_erase_animation()
        self._text.configure(fg=COLOUR_ERASED)
        self._root.after(700, self._complete_erase)

    def _complete_erase(self) -> None:
        """Wipe text buffer and return to idle state."""
        self._text.delete("1.0", "end")
        self._text.configure(fg=COLOUR_SAFE, insertbackground=COLOUR_SAFE)
        self._timer.disarm()
        self._state.reset()
        self._refresh_bar(1.0, COLOUR_BAR_SAFE)
        self._lbl_timer.configure(text="", text_color=COLOUR_SAFE)
        self._lbl_counts.configure(text="0 words  ·  0 chars", text_color="#484F58")
        self._lbl_session.configure(
            text="Start typing — stop for 5 seconds and everything is gone.",
            text_color="#8B949E",
        )
        self._show_idle_overlay(erased=True)
        self._text.focus_set()

    def _on_resize(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        """Keep the progress bar fill correctly sized after window resize."""
        self._root.after_idle(
            lambda: self._refresh_bar(self._state.progress_ratio)
        )

    def _on_close(self) -> None:
        """Cleanly cancel all pending callbacks before destroying the window."""
        self._timer.disarm()
        self._root.destroy()

    # ------------------------------------------------------------------
    # Visual Feedback
    # ------------------------------------------------------------------

    def _refresh_feedback(self) -> None:
        """
        Update every visual indicator to reflect the current :class:`AppState`.
        Called both by timer ticks and immediately after each keystroke.
        """
        ratio = self._state.progress_ratio
        seconds_left = ratio * (IDLE_TIMEOUT_MS / 1_000)

        # Severity colour thresholds
        if ratio > 0.50:
            bar_colour = COLOUR_BAR_SAFE
            text_colour = COLOUR_SAFE
            count_colour = "#6E7681"
        elif ratio > 0.20:
            bar_colour = COLOUR_BAR_WARNING
            text_colour = COLOUR_WARNING
            count_colour = COLOUR_WARNING
        else:
            bar_colour = COLOUR_BAR_DANGER
            text_colour = COLOUR_DANGER
            count_colour = COLOUR_DANGER

        self._refresh_bar(ratio, bar_colour)
        self._text.configure(fg=text_colour, insertbackground=text_colour)

        # Countdown timer display
        if self._state.phase == SessionPhase.ACTIVE:
            self._lbl_timer.configure(
                text=f"⏱  {seconds_left:.1f}s remaining",
                text_color=text_colour,
            )
        else:
            self._lbl_timer.configure(text="")

        # Word / char counts
        self._lbl_counts.configure(
            text=(
                f"{self._state.word_count:,} words  ·  "
                f"{self._state.char_count:,} chars"
            ),
            text_color=count_colour,
        )

        # Status hint
        if self._state.phase == SessionPhase.ACTIVE:
            if ratio <= 0.20:
                hint = "🔥  KEEP TYPING — you're about to lose everything!"
            elif ratio <= 0.50:
                hint = "⚠️  Don't stop now..."
            else:
                hint = f"✍️  Best this session: {self._state.session_best:,} words"
            self._lbl_session.configure(text=hint, text_color=text_colour)

    def _refresh_bar(
        self,
        ratio: float,
        colour: Optional[str] = None,
    ) -> None:
        """
        Redraw the danger progress bar to match *ratio* (0.0-1.0).

        Args:
            ratio:  Remaining time fraction.
            colour: Optional override for the fill colour.
        """
        try:
            total_width = self._bar_canvas.winfo_width()
        except tk.TclError:
            return

        fill_width = max(0, int(total_width * ratio))

        if colour is not None:
            self._bar_canvas.itemconfig(self._bar_fill, fill=colour)

        self._bar_canvas.coords(self._bar_fill, 0, 0, fill_width, BAR_HEIGHT)

    def _flash_erase_animation(self) -> None:
        """
        Four-pulse red flash animation on the progress bar track.
        Signals imminent deletion before the buffer is wiped.
        """
        pulses = [COLOUR_DANGER, COLOUR_BG, COLOUR_DANGER, COLOUR_BG,
                  COLOUR_DANGER, COLOUR_BG]
        for i, col in enumerate(pulses):
            self._root.after(
                i * 100,
                lambda c=col: self._bar_canvas.configure(bg=c),
            )
        # Restore track background after animation
        self._root.after(
            len(pulses) * 100 + 50,
            lambda: self._bar_canvas.configure(bg=COLOUR_BAR_BG),
        )

    # ------------------------------------------------------------------
    # Overlay helpers
    # ------------------------------------------------------------------

    def _show_idle_overlay(self, *, erased: bool = False) -> None:
        """
        Display the translucent on-screen guide overlay.

        Args:
            erased: If True, show the post-erase copy; otherwise show welcome copy.
        """
        if erased:
            msg = (
                "💀   Everything is gone.\n\n"
                "You stopped.\n\n"
                "Start again — if you dare."
            )
        else:
            msg = (
                "THE MOST DANGEROUS WRITING APP\n\n"
                "Start typing to begin your session.\n\n"
                "If you stop for 5 seconds,\n"
                "everything you have written\n"
                "will be permanently deleted."
            )

        self._overlay_label.configure(text=msg)
        self._overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        # Text widget keeps focus so keystrokes are never lost
        self._text.focus_set()

    def _hide_overlay(self) -> None:
        """Remove the overlay to reveal the clean writing surface."""
        self._overlay.place_forget()
        self._text.focus_set()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the Tk main event loop."""
        self._root.mainloop()


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Application entry point.

    Constructs :class:`WritingApp` — which internally wires Model, View, and
    Controller — then hands control to the Tk event loop.
    """
    app = WritingApp()
    app.run()


if __name__ == "__main__":
    main()
