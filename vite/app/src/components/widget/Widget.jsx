// -----------------------------------------------------------
//  [*] Widget — dashboard stat card
//
//  White stat card of the dashboard: a label, a big number
//  and an icon in the corner. `link` makes the corner icon a
//  router link to the matching list page.
//
//  A non-numeric count renders as empty, so the card keeps
//  its shape while the data is still loading.
//
//  Used by:
//    - Home — every counter card on the dashboard
// -----------------------------------------------------------

import { Link } from "react-router-dom";


export default function Widget({ text, bottomtext, count, icon, link }) {

  return (
    <div
      className="flex justify-between flex-1 rounded-[15px] w-full bg-white shadow-card"
      style={{ height: '120px', padding: '15px' }}
    >

      {/* Label + number */}
      <div className="flex flex-col justify-between">
        <span className="font-bold text-sm text-[rgb(160,160,160)]">{text}</span>
        <span className="text-[28px] font-light">
          {isNaN(count) ? "" : count}
        </span>
        <span className="w-max text-xs border-b border-gray-500">{bottomtext}</span>
      </div>

      {/* Corner icon, optionally linking to a page */}
      <div className="flex flex-col justify-end mr-2.5">
        {link ?
          <Link to={link} style={{ textDecoration: "none", margin: '0px', padding: '0px' }}>
            <>{icon}</>
          </Link>
        :
          <div className="self-end">
            <>{icon}</>
          </div>
        }
      </div>

    </div>
  );
}
