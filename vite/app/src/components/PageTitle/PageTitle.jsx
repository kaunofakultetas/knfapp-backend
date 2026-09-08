// -----------------------------------------------------------
//  [*] PageTitle — the standard page heading row
//
//  The grey 24px heading a page shows above its content. Also
//  a flex row with justify-between: put buttons or extras in
//  as siblings of the text and they land on the right edge.
//
//  Used by:
//    - every page under systemPages except Login
// -----------------------------------------------------------

export default function PageTitle({ children }) {
  return (
    <div className="w-full text-2xl text-gray-500 mb-2.5 flex items-center justify-between">
      {children}
    </div>
  );
}
