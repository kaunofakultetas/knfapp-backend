// -----------------------------------------------------------
//  [*] LongPressButton — hold-to-confirm button
//
//  Button that must be held down (default 3 s) before it
//  fires — used for destructive actions instead of a confirm
//  dialog. While holding, the label is replaced by a circular
//  progress ring; releasing early cancels and can show an
//  error toast, completing can show a success toast.
//
//  Works with both mouse and touch. Split into small pieces
//  (main component near the end):
//
//    useLongPress           — press state + rAF progress loop
//    PressProgress          — circular progress ring
//    ButtonTooltip          — tooltip that works when disabled
//    LongPressButton        — the button itself (default export)
//    LongPressDeleteButton  — red "delete" preset
//
//  Imported via the folder's index.js:
//    @/components/LongPressButton
// -----------------------------------------------------------

import { useState, useEffect } from "react";
import { Button, CircularProgress, Tooltip } from "@mui/material";
import toast from 'react-hot-toast';


const DEFAULT_DURATION = 3000; // 3 seconds







// -----------------------------------------------------------
// useLongPress
// -----------------------------------------------------------
//
// All the press logic. The only real state is `pressedAt` —
// the timestamp when the hold started (null = not pressed).
// While it is set, an effect runs a requestAnimationFrame
// loop that updates the 0–100 progress and fires onComplete
// (+ optional success toast) once the duration is reached.
//
// Releasing early just clears `pressedAt` (+ optional error
// toast); the effect cleanup stops the animation loop, which
// also covers unmount mid-press.
//
// Returns { isPressed, progress, start, cancel } — wire start
// to mouse/touch "down" events and cancel to "up"/"leave".
//
// Used by:
//   - LongPressButton (below)
// -----------------------------------------------------------

function useLongPress({ onComplete, disabled, duration, completedToastMessage, uncompletedToastMessage }) {

  const [pressedAt, setPressedAt] = useState(null);
  const [progress, setProgress] = useState(0);


  // Animation loop — runs only while the button is held
  useEffect(() => {
    if (pressedAt === null) return;

    let frame;
    const tick = () => {
      const elapsed = Date.now() - pressedAt;
      setProgress(Math.min((elapsed / duration) * 100, 100));

      if (elapsed >= duration) {
        setPressedAt(null);
        setProgress(0);
        if (completedToastMessage) {
          toast.success(<b>{completedToastMessage}</b>, { duration: 3000 });
        }
        onComplete?.();
        return;
      }

      frame = requestAnimationFrame(tick);
    };

    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [pressedAt, duration, onComplete, completedToastMessage]);


  // Press started (mouse down / touch start)
  const start = (e) => {
    if (disabled) return;
    e.stopPropagation();
    e.preventDefault();

    setProgress(0);
    setPressedAt(Date.now());
  };


  // Press released or pointer left the button early
  const cancel = (e) => {
    if (pressedAt === null) return;
    e?.stopPropagation();

    if (Date.now() - pressedAt < duration && uncompletedToastMessage) {
      toast.error(<b>{uncompletedToastMessage}</b>, { duration: 3000 });
    }

    setPressedAt(null);
    setProgress(0);
  };


  return { isPressed: pressedAt !== null, progress, start, cancel };
}







// -----------------------------------------------------------
// PressProgress
// -----------------------------------------------------------
//
// The circular progress ring shown inside the button while
// it is held: a faint full circle in the background with the
// actual progress drawn on top. Transitions are disabled so
// the rAF-driven value animates smoothly.
//
// Used by:
//   - LongPressButton (below) — default pressed content
// -----------------------------------------------------------

function PressProgress({ progress, size, thickness, color, bgColor }) {
  return (
    <div
      style={{
        position: 'relative',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: size,
        height: size,
      }}
    >
      {/* Background circle */}
      <CircularProgress
        variant="determinate"
        value={100}
        size={size}
        thickness={thickness}
        sx={{ color: bgColor, position: 'absolute' }}
      />

      {/* Progress circle */}
      <CircularProgress
        variant="determinate"
        value={progress}
        size={size}
        thickness={thickness}
        sx={{
          color: color,
          position: 'absolute',
          '& .MuiCircularProgress-circle': {
            strokeLinecap: 'round',
            transition: 'none',
          },
        }}
      />
    </div>
  );
}







// -----------------------------------------------------------
// ButtonTooltip
// -----------------------------------------------------------
//
// Black tooltip around the button. Renders the button as-is
// when no tooltip text is given; otherwise wraps it in a
// <span> so the tooltip also works on disabled buttons
// (disabled elements don't fire hover events themselves).
//
// Used by:
//   - LongPressButton (below)
// -----------------------------------------------------------

function ButtonTooltip({ tooltip, fullWidth, children }) {

  if (!tooltip) return children;

  return (
    <Tooltip
      title={<span style={{ fontSize: '0.9rem' }}>{tooltip}</span>}
      arrow
      disableInteractive
      slotProps={{
        tooltip: { sx: { backgroundColor: 'black', py: 0.5, px: 1 } },
        arrow: { sx: { color: 'black' } },
      }}
    >
      {/* Stretches only for fullWidth buttons — otherwise it
          must not grab flex space from its siblings */}
      <span style={{ display: 'inline-flex', flex: fullWidth ? 1 : 'none', width: fullWidth ? '100%' : 'auto' }}>
        {children}
      </span>
    </Tooltip>
  );
}







// -----------------------------------------------------------
// LongPressButton (default export)
// -----------------------------------------------------------
//
// The button itself — wires the useLongPress handlers to the
// mouse/touch events. While held, the label is hidden (but
// kept in the layout so the button size never changes) and
// the progress ring (or custom pressedContent) is overlaid
// centered on top.
//
// Used by:
//   - LongPressDeleteButton (below)
//   - Account — the "log out everywhere" button
// -----------------------------------------------------------

export default function LongPressButton({
  // Core functionality
  onComplete,              // Called when long press completes
  disabled = false,
  duration = DEFAULT_DURATION,

  // Button content
  children,                // Button content when not pressed
  pressedContent,          // Custom content while pressing (optional)

  // Appearance
  color = "error",         // MUI color: "primary" | "secondary" | "error" | "warning" | "info" | "success"
  variant = "contained",   // MUI variant: "contained" | "outlined" | "text"
  fullWidth = false,
  size = "medium",         // MUI size: "small" | "medium" | "large"
  sx = {},

  // Feedback
  completedToastMessage,
  uncompletedToastMessage,

  // Tooltip (shown on hover, works even when disabled)
  tooltip = "",

  // Progress indicator customization
  progressSize = 24,
  progressThickness = 4,
  progressColor = "white",
  progressBgColor = "rgba(255,255,255,0.3)",

  // Other props passed to Button
  ...buttonProps
}) {

  const { isPressed, progress, start, cancel } = useLongPress({
    onComplete,
    disabled,
    duration,
    completedToastMessage,
    uncompletedToastMessage,
  });

  return (
    <ButtonTooltip tooltip={tooltip} fullWidth={fullWidth}>
      <Button
        variant={variant}
        color={color}
        fullWidth={fullWidth}
        size={size}
        disabled={disabled}
        onMouseDown={start}
        onMouseUp={cancel}
        onMouseLeave={cancel}
        onTouchStart={start}
        onTouchEnd={cancel}
        onContextMenu={(e) => e.preventDefault()} // Prevent right-click menu on long press
        sx={{
          userSelect: 'none', // Prevent text selection while holding
          ...sx,
        }}
        {...buttonProps}
      >
        {/* The idle content always stays in the layout (only
            hidden while pressed) so the button keeps its exact
            size; the progress ring is overlaid centered on top */}
        <span style={{ display: 'inline-flex', alignItems: 'center', visibility: isPressed ? 'hidden' : 'visible' }}>
          {children}
        </span>

        {isPressed && (
          <span style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}>
            {pressedContent || (
              <PressProgress
                progress={progress}
                size={progressSize}
                thickness={progressThickness}
                color={progressColor}
                bgColor={progressBgColor}
              />
            )}
          </span>
        )}
      </Button>
    </ButtonTooltip>
  );
}







// -----------------------------------------------------------
// LongPressDeleteButton (exported)
// -----------------------------------------------------------
//
// Preset: red hold-to-delete button.
//
// Used by:
//   - EditUser — the account-erasure button
//   - InvitationDetails — the revoke button
//   - PostDetails — the delete-post button
// -----------------------------------------------------------

export function LongPressDeleteButton({
  children,
  completedToastMessage,
  uncompletedToastMessage,
  ...props
}) {
  return (
    <LongPressButton
      color="error"
      completedToastMessage={completedToastMessage}
      uncompletedToastMessage={uncompletedToastMessage}
      {...props}
    >
      {children}
    </LongPressButton>
  );
}
