// -----------------------------------------------------------
//  [*] UniversalModal — the shared modal dialog
//
//  One configurable component covering the common cases:
//  title/description header with an optional variant icon,
//  arbitrary children as the body, and either the standard
//  Confirm/Cancel pair or fully custom action buttons.
//
//  Variants ("default" | "danger" | "warning" | "info" |
//  "success") pick the header icon and the confirm button
//  color. The standard button labels come from the
//  COMPONENTS.universalModal translations unless the caller
//  passes its own.
//
//  Split into (main component last):
//
//    VARIANTS         — per-variant icon + colors
//    ModalHeader      — icon, title, description, close (×)
//    StandardActions  — the Confirm/Cancel button bar
//    UniversalModal   — the modal itself (default export)
//
//  Imported via the folder's index.js:
//    @/components/UniversalModal
// -----------------------------------------------------------

import { Modal, Paper, Box, Typography, Button, IconButton, CircularProgress, Divider } from "@mui/material";
import CloseIcon from '@mui/icons-material/Close';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import CheckCircleOutlineIcon from '@mui/icons-material/CheckCircleOutline';

import { useTranslations } from '@/i18n';


// Variant configurations — header icon, confirm button color
const VARIANTS = {
  default: {
    icon: null,
    confirmColor: "primary",
    iconColor: "primary",
  },
  danger: {
    icon: ErrorOutlineIcon,
    confirmColor: "error",
    iconColor: "error",
  },
  warning: {
    icon: WarningAmberIcon,
    confirmColor: "warning",
    iconColor: "warning",
  },
  info: {
    icon: InfoOutlinedIcon,
    confirmColor: "info",
    iconColor: "info",
  },
  success: {
    icon: CheckCircleOutlineIcon,
    confirmColor: "success",
    iconColor: "success",
  },
};







// -----------------------------------------------------------
// ModalHeader
// -----------------------------------------------------------
//
// Top section: the variant icon, title and description on the
// left, the close (×) button on the right. Bottom padding is
// tighter when something follows underneath.
//
// Used by:
//   - UniversalModal (below)
// -----------------------------------------------------------

function ModalHeader({ icon: Icon, iconColor, title, description, hasBody, showCloseButton, onClose }) {
  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'space-between',
        p: 3,
        pb: description || hasBody ? 2 : 3,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flex: 1, marginBottom: 1 }}>
        {Icon && (
          <Icon
            color={iconColor}
            sx={{ fontSize: 28 }}
          />
        )}
        <Box>
          {title && (
            <Typography
              id="universal-modal-title"
              variant="h6"
              component="h2"
              sx={{ fontWeight: 600 }}
            >
              {title}
            </Typography>
          )}
          {description && (
            <Typography
              id="universal-modal-description"
              variant="body2"
              color="text.secondary"
              sx={{ mt: 0.5 }}
            >
              {description}
            </Typography>
          )}
        </Box>
      </Box>

      {showCloseButton && (
        <IconButton
          onClick={onClose}
          size="small"
          sx={{
            ml: 1,
            color: 'text.secondary',
            '&:hover': { color: 'error.main' }
          }}
        >
          <CloseIcon fontSize="small" />
        </IconButton>
      )}
    </Box>
  );
}







// -----------------------------------------------------------
// StandardActions
// -----------------------------------------------------------
//
// The default footer: right-aligned Cancel + Confirm buttons.
// While `loading` both are disabled and the confirm button
// shows a spinner.
//
// Used by:
//   - UniversalModal (below) — when no custom `actions` given
// -----------------------------------------------------------

function StandardActions({ showCancel, cancelText, onCancel, showConfirm, confirmText, confirmColor, onConfirm, confirmDisabled, loading }) {
  return (
    <>
      <Divider />
      <Box
        sx={{
          display: 'flex',
          justifyContent: 'flex-end',
          gap: 1.5,
          p: 2,
          px: 3,
        }}
      >
        {showCancel && (
          <Button
            variant="outlined"
            onClick={onCancel}
            disabled={loading}
          >
            {cancelText}
          </Button>
        )}
        {showConfirm && (
          <Button
            variant="contained"
            color={confirmColor}
            onClick={onConfirm}
            disabled={confirmDisabled || loading}
            startIcon={loading ? <CircularProgress size={16} color="inherit" /> : null}
          >
            {confirmText}
          </Button>
        )}
      </Box>
    </>
  );
}







// -----------------------------------------------------------
// UniversalModal (default export)
// -----------------------------------------------------------
//
// Used by:
//   - the add/edit/detail dialogs of the list pages (users,
//     invitations, reports, news, wayfind)
// -----------------------------------------------------------

export default function UniversalModal({
  // Control
  open,
  onClose,

  // Content
  title,
  description,
  children,

  // Actions - custom JSX for buttons (overrides confirm/cancel)
  actions,

  // Standard action buttons (used when 'actions' prop is not provided)
  confirmText,
  cancelText,
  onConfirm,
  onCancel,
  showCancel = true,
  showConfirm = true,
  confirmDisabled = false,

  // Variants: "default" | "danger" | "warning" | "info" | "success"
  variant = "default",

  // Sizing
  maxWidth = 500,
  fullWidth = false,

  // Loading state
  loading = false,

  // Behavior
  closeOnConfirm = true,
  closeOnBackdropClick = true,
  showCloseButton = true,

  // Custom styling
  sx = {},
  contentSx = {},
}) {

  const t = useTranslations("COMPONENTS.universalModal");
  const variantConfig = VARIANTS[variant] || VARIANTS.default;



  // onConfirm is awaited so closeOnConfirm doesn't shut the modal
  // before an async confirm (e.g. an API call) has finished
  const handleConfirm = async () => {
    if (onConfirm) {
      await onConfirm();
    }
    if (closeOnConfirm) {
      onClose?.();
    }
  };

  const handleCancel = () => {
    if (onCancel) {
      onCancel();
    }
    onClose?.();
  };

  // MUI reports why the modal wants to close; ignore backdrop
  // clicks when closeOnBackdropClick is off (Esc still closes)
  const handleBackdropClick = (event, reason) => {
    if (reason === 'backdropClick' && !closeOnBackdropClick) {
      return;
    }
    onClose?.();
  };


  const showActionBar = actions || showConfirm || showCancel;


  return (
    <Modal
      open={open}
      onClose={handleBackdropClick}
      aria-labelledby="universal-modal-title"
      aria-describedby="universal-modal-description"
      slotProps={{
        backdrop: {
          sx: {
            backgroundColor: 'rgba(0, 0, 0, 0.15)',
            backdropFilter: 'blur(6px)',
          },
        },
      }}
    >
      <Paper
        sx={{
          position: 'absolute',
          top: '50%',
          left: '50%',
          transform: 'translate(-50%, -50%)',
          width: fullWidth ? '90%' : 'auto',
          maxWidth: maxWidth,
          minWidth: 300,
          maxHeight: '90vh',
          overflow: 'auto',
          borderRadius: 2,
          boxShadow: 24,
          outline: 'none',   // Modal focuses the Paper; hide the focus ring
          ...sx,
        }}
      >
        <ModalHeader
          icon={variantConfig.icon}
          iconColor={variantConfig.iconColor}
          title={title}
          description={description}
          hasBody={Boolean(children)}
          showCloseButton={showCloseButton}
          onClose={onClose}
        />

        {/* Content */}
        {children && (
          <Box sx={{ px: 3, pb: showActionBar ? 2 : 3, ...contentSx }}>
            {children}
          </Box>
        )}

        {/* Custom actions — block layout so callers can use full-width buttons */}
        {actions && (
          <>
            <Divider />
            <Box sx={{ p: 2, px: 3 }}>
              {actions}
            </Box>
          </>
        )}

        {/* Standard Confirm/Cancel bar — only when no custom actions */}
        {!actions && (showConfirm || showCancel) && (
          <StandardActions
            showCancel={showCancel}
            cancelText={cancelText ?? t("cancel")}
            onCancel={handleCancel}
            showConfirm={showConfirm}
            confirmText={confirmText ?? t("confirm")}
            confirmColor={variantConfig.confirmColor}
            onConfirm={handleConfirm}
            confirmDisabled={confirmDisabled}
            loading={loading}
          />
        )}
      </Paper>
    </Modal>
  );
}
