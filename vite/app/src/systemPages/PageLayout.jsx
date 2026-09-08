// -----------------------------------------------------------
//  [*] SystemPages — PageLayout
//
//  The shared frame of every page: the Navbar on top, the
//  Sidebar on the left, and the page content in a scrollable
//  area next to it. Also mounts the react-hot-toast
//  <Toaster> that the forms use for save/delete feedback.
//
//  `backgroundColor` tints the content area (Home passes the
//  grey #EBECEF, tables stay white).
//
//  Used by:
//    - every systemPages/* page except Login
// -----------------------------------------------------------

import Sidebar from "@/components/sidebar/Sidebar";
import Navbar from "@/components/navbar/Navbar";

import { Toaster } from 'react-hot-toast';


export default function PageLayout({ children, authData, backgroundColor = 'white' }) {
  return (
    <div className="h-screen flex flex-col">
      <Navbar authData={authData} />
      <Toaster position="top-center" containerStyle={{ top: 20 }} toastOptions={{ style: { textAlign: 'center' } }}/>
      <div className="flex flex-1 overflow-hidden">
        <Sidebar authData={authData}/>
        <div className="flex-1 overflow-auto" style={{ backgroundColor }}>
          {children}
        </div>
      </div>
    </div>
  );
}
