import { CircleUserRound } from "lucide-react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../lib/auth";
import { useTheme } from "../lib/theme";
import { DESKTOP_QUERY, useMediaQuery } from "../lib/useMediaQuery";
import { IconButton } from "./ui/Button";
import { NotificationsBell } from "./watch/NotificationsBell";
import {
  DropdownMenu,
  MenuItem,
  MenuLabel,
  MenuRadioGroup,
  MenuSeparator,
} from "./ui/DropdownMenu";

const SIGN_IN =
  "inline-flex h-9 items-center rounded-control border border-border-strong px-3.5 text-sm " +
  "font-medium transition-colors hover:bg-surface-2 active:bg-surface-3";

/** Everything personal lives here: who is signed in, the theme, and, on phones, the top-level
 *  pages that do not fit in the bar. Present for signed-out visitors too, so the theme switch and
 *  mobile navigation are always reachable. */
export default function AccountMenu() {
  const { user, ready, logout } = useAuth();
  const { theme, setTheme } = useTheme();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const desktop = useMediaQuery(DESKTOP_QUERY);

  // The session is still being restored: reserve the space rather than flash "Sign in".
  if (!ready) return <div aria-hidden className="size-9" />;

  const signIn = { pathname: "/login" };
  const from = { from: pathname };

  return (
    <div className="flex items-center gap-2">
      {user && <NotificationsBell userId={user.id} />}
      {!user && desktop && (
        <Link to={signIn} state={from} className={SIGN_IN}>
          Sign in
        </Link>
      )}
      <DropdownMenu
        trigger={
          user ? (
            <button
              type="button"
              aria-label="Account menu"
              className="flex size-9 items-center justify-center rounded-full bg-surface-2 text-label font-medium uppercase text-text transition-colors hover:bg-surface-3"
            >
              {user.email.charAt(0)}
            </button>
          ) : (
            <IconButton label="Menu">
              <CircleUserRound aria-hidden className="size-5" />
            </IconButton>
          )
        }
      >
        {user ? (
          <>
            <MenuLabel>{user.email}</MenuLabel>
            <MenuItem onSelect={() => navigate("/research")}>Your research</MenuItem>
            <MenuItem onSelect={() => navigate("/alerts")}>Alerts</MenuItem>
          </>
        ) : (
          !desktop && <MenuItem onSelect={() => navigate(signIn, { state: from })}>Sign in</MenuItem>
        )}
        {!desktop && (
          <>
            <MenuItem onSelect={() => navigate("/compare")}>Compare</MenuItem>
            <MenuItem onSelect={() => navigate("/portfolio")}>Portfolio</MenuItem>
          </>
        )}
        {(user || !desktop) && <MenuSeparator />}
        <MenuLabel>Theme</MenuLabel>
        <MenuRadioGroup
          label="Theme"
          value={theme}
          onValueChange={setTheme}
          options={[
            { value: "dark", label: "Dark" },
            { value: "light", label: "Light" },
          ]}
        />
        {user && (
          <>
            <MenuSeparator />
            <MenuItem onSelect={() => void logout()}>Sign out</MenuItem>
          </>
        )}
      </DropdownMenu>
    </div>
  );
}
