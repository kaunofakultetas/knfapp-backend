// -----------------------------------------------------------
//  [*] Sidebar — left navigation
//
//  Left navigation of the panel, collapsible (the state
//  persists in localStorage under "sidebarOpen"). While
//  collapsed only the icons remain and labels show up as
//  black tooltips.
//
//  The links live in the SECTIONS table — one entry per
//  group, labels resolved from the "sidebar" translation
//  namespace. A `roles` key on an item gates it by
//  authData.role (curators see a reduced menu: no user
//  directory, no broadcast, no scrapers, no DB console),
//  `external` renders a plain <a target="_blank"> instead of
//  a router link (the sibling infra tools live outside the
//  /adminpanel prefix). The row the current route matches is
//  highlighted in the brand tint.
//
//  Split into (root component last):
//
//    SECTIONS        — the declarative link table
//    SectionTitle    — grey group label
//    MenuItemContent — icon + label + collapsed tooltip
//    MenuItem        — router <Link> or external <a>
//    Sidebar         — the sidebar itself (default export)
// -----------------------------------------------------------

import { useState } from "react";
import { Link, useLocation } from "react-router-dom";
import Tooltip from '@mui/material/Tooltip';

import { useTranslations } from '@/i18n';

// Collapse/Expand Sidebar
import KeyboardDoubleArrowLeftIcon from '@mui/icons-material/KeyboardDoubleArrowLeft';
import KeyboardDoubleArrowRightIcon from '@mui/icons-material/KeyboardDoubleArrowRight';

// HOME
import DashboardIcon from "@mui/icons-material/Dashboard";

// MANAGEMENT
import PersonOutlineIcon from "@mui/icons-material/PersonOutline";
import VpnKeyOutlinedIcon from '@mui/icons-material/VpnKeyOutlined';
import FlagOutlinedIcon from '@mui/icons-material/FlagOutlined';
import CampaignOutlinedIcon from '@mui/icons-material/CampaignOutlined';
import HistoryOutlinedIcon from '@mui/icons-material/HistoryOutlined';

// CONTENT
import ArticleOutlinedIcon from '@mui/icons-material/ArticleOutlined';
import CalendarMonthOutlinedIcon from '@mui/icons-material/CalendarMonthOutlined';
import MenuBookOutlinedIcon from '@mui/icons-material/MenuBookOutlined';
import ImageOutlinedIcon from '@mui/icons-material/ImageOutlined';
import FolderOutlinedIcon from '@mui/icons-material/FolderOutlined';
import LinkOffOutlinedIcon from '@mui/icons-material/LinkOffOutlined';

// OPERATIONS
import CloudSyncOutlinedIcon from '@mui/icons-material/CloudSyncOutlined';
import MapOutlinedIcon from '@mui/icons-material/MapOutlined';

// SYSTEM
import ApiIcon from '@mui/icons-material/Api';
import StorageIcon from '@mui/icons-material/Storage';
import PersonIcon from '@mui/icons-material/Person';
import ExitToAppIcon from "@mui/icons-material/ExitToApp";


// The whole menu as data. `roles` limits an item to those
// roles (absent = every panel role); `external` links leave
// the SPA (the infra tools live on the origin root, outside
// the /adminpanel prefix, so they must be plain <a> hrefs).
const SECTIONS = [
  {
    title: "HOME.TITLE",
    items: [
      { href: "/", icon: DashboardIcon, label: "HOME.home" },
    ],
  },
  {
    title: "MANAGEMENT.TITLE",
    items: [
      { href: "/users", icon: PersonOutlineIcon, label: "MANAGEMENT.users", roles: ["admin"] },
      { href: "/invitations", icon: VpnKeyOutlinedIcon, label: "MANAGEMENT.invitations" },
      { href: "/reports", icon: FlagOutlinedIcon, label: "MANAGEMENT.reports" },
      { href: "/broadcast", icon: CampaignOutlinedIcon, label: "MANAGEMENT.broadcast", roles: ["admin"] },
      { href: "/audit", icon: HistoryOutlinedIcon, label: "MANAGEMENT.audit", roles: ["admin"] },
    ],
  },
  {
    title: "CONTENT.TITLE",
    items: [
      { href: "/news", icon: ArticleOutlinedIcon, label: "CONTENT.news" },
      { href: "/memes", icon: ImageOutlinedIcon, label: "CONTENT.memes" },
      { href: "/uploads", icon: FolderOutlinedIcon, label: "CONTENT.uploads", roles: ["admin"] },
      { href: "/tombstones", icon: LinkOffOutlinedIcon, label: "CONTENT.tombstones", roles: ["admin"] },
      { href: "/schedule", icon: CalendarMonthOutlinedIcon, label: "CONTENT.schedule" },
      { href: "/info", icon: MenuBookOutlinedIcon, label: "CONTENT.info" },
    ],
  },
  {
    title: "OPERATIONS.TITLE",
    items: [
      { href: "/scrapers", icon: CloudSyncOutlinedIcon, label: "OPERATIONS.scrapers", roles: ["admin"] },
      { href: "/wayfind", icon: MapOutlinedIcon, label: "OPERATIONS.wayfind" },
    ],
  },
  {
    title: "SYSTEM.TITLE",
    items: [
      { href: "/swagger", icon: ApiIcon, label: "SYSTEM.apidocs", external: true },
      { href: "/dbgate", icon: StorageIcon, label: "SYSTEM.database", external: true, roles: ["admin"] },
      { href: "/account", icon: PersonIcon, label: "SYSTEM.account" },
      { href: "/login", icon: ExitToAppIcon, label: "SYSTEM.logout" },
    ],
  },
];







// -----------------------------------------------------------
// SectionTitle
// -----------------------------------------------------------
//
// Grey uppercase label above a group of menu items; shrinks
// to a dashed placeholder while the sidebar is collapsed.
//
// Used by:
//   - Sidebar (below) — one per menu section
// -----------------------------------------------------------

function SectionTitle({ title, open }) {
  return (
    <p className="text-[10px] font-bold text-[#999] mt-[15px] mb-[2px] whitespace-pre-wrap">
      {open ? title : '-----'}
    </p>
  );
}







// -----------------------------------------------------------
// MenuItemContent
// -----------------------------------------------------------
//
// One row of the menu: burgundy icon + label. While the
// sidebar is collapsed the label is hidden and shown as a
// black tooltip on hover instead. The active row keeps a
// standing brand tint.
//
// Used by:
//   - MenuItem (below)
// -----------------------------------------------------------

function MenuItemContent({ icon: Icon, label, open, active }) {

  const [hovered, setHovered] = useState(false);

  return (
    <Tooltip
      open={!open && hovered}
      title={label}
      placement="right"
      disableInteractive
      slotProps={{
        tooltip: { sx: { backgroundColor: '#000', fontSize: '15px' } },
      }}
    >
      <li
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        onClick={() => setHovered(false)}
        className={`flex items-center py-[1px] pl-[6px] pr-[10px] cursor-pointer whitespace-nowrap rounded-[3px] transition-colors hover:bg-[#999] ${active ? 'bg-primary/10' : ''}`}
      >
        <Icon className="text-[17px] text-primary" />
        {open && <span className="text-[13px] font-semibold text-[rgb(65,65,65)] ml-[10px]">{label}</span>}
      </li>
    </Tooltip>
  );
}







// -----------------------------------------------------------
// MenuItem
// -----------------------------------------------------------
//
// Wraps MenuItemContent in a router <Link>, or in a plain
// <a target="_blank"> for the external tools (swagger,
// dbgate).
//
// Used by:
//   - Sidebar (below) — one per menu entry
// -----------------------------------------------------------

function MenuItem({ href, icon: Icon, label, open, active, external = false }) {

  if (external) {
    return (
      <a href={href} className="no-underline" target="_blank" rel="noopener noreferrer">
        <MenuItemContent icon={Icon} label={label} open={open} active={active} />
      </a>
    );
  }

  return (
    <Link to={href} className="no-underline">
      <MenuItemContent icon={Icon} label={label} open={open} active={active} />
    </Link>
  );
}







// -----------------------------------------------------------
// Sidebar (default export)
// -----------------------------------------------------------
//
// Used by:
//   - PageLayout — every page
// -----------------------------------------------------------

export default function Sidebar({ authData }) {

  const t = useTranslations("sidebar");
  const { pathname } = useLocation();

  // Collapsed/expanded — remembered across page loads; on
  // narrow screens it starts collapsed to leave room for the
  // page content (it can still be expanded by hand)
  const [open, setOpen] = useState(() =>
    window.innerWidth >= 768 && localStorage.getItem("sidebarOpen") !== "false"
  );

  const toggleOpen = () => {
    const sidebarOpenNewValue = !open;
    setOpen(sidebarOpenNewValue);
    localStorage.setItem('sidebarOpen', sidebarOpenNewValue);
  };


  // Role gate — while the session check is still running,
  // role-limited items stay hidden (the menu grows once
  // authData arrives)
  const visible = (item) => !item.roles || item.roles.includes(authData?.role);


  return (
    <div className="border-r border-r-white bg-white min-h-full overflow-y-auto transition-all duration-500 ease-in-out">
      <div className="px-[10px]">
        <ul className="list-none m-0 p-0">

          {/* Collapse Button */}
          <button
            className="text-[#B2BAC2] bg-primary cursor-pointer mt-5 border-0 rounded-lg w-full"
            onClick={toggleOpen}
          >
            {open
              ? <KeyboardDoubleArrowLeftIcon className="align-middle" />
              : <KeyboardDoubleArrowRightIcon className="align-middle" />
            }
          </button>

          {SECTIONS.map((section) => {
            const items = section.items.filter(visible);
            if (items.length === 0) return null;

            return (
              <div key={section.title}>
                <SectionTitle title={t(section.title)} open={open} />
                {items.map((item) => (
                  <MenuItem
                    key={item.href}
                    href={item.href}
                    icon={item.icon}
                    label={t(item.label)}
                    open={open}
                    active={pathname === item.href}
                    external={item.external}
                  />
                ))}
              </div>
            );
          })}

        </ul>
      </div>
    </div>
  );
}
