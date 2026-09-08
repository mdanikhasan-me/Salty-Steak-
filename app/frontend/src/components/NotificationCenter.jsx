import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";
import { useAppState } from "../state/AppState.jsx";
import { notificationVisibleOnPage } from "../workflows/notifications.mjs";

const icons = {
  success: CheckCircle2,
  error: AlertCircle,
  information: Info,
};

export function NotificationCenter({ page }) {
  const { notifications, dismissNotification, pauseNotification, resumeNotification } = useAppState();
  const visibleNotifications = notifications.filter((notification) =>
    notificationVisibleOnPage(notification, page),
  ).slice(-1);
  return (
    <div className="notification-region" aria-live="polite" aria-label="Notifications">
      {visibleNotifications.map((notification) => {
        const Icon = icons[notification.kind] || Info;
        return (
          <div
            className={`notification notification--${notification.kind || "information"}`}
            key={notification.id}
            onMouseEnter={() => pauseNotification(notification.id)}
            onMouseLeave={() => resumeNotification(notification.id)}
            onFocus={() => pauseNotification(notification.id)}
            onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget)) resumeNotification(notification.id);
            }}
          >
            <Icon aria-hidden="true" />
            <p>{notification.message}</p>
            <button
              type="button"
              className="notification-close"
              aria-label="Dismiss notification"
              onClick={() => dismissNotification(notification.id)}
            >
              <X aria-hidden="true" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
