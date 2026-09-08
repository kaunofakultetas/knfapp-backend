// -----------------------------------------------------------
//  [*] Login page — BouncingDotsLoader
//
//  Three small white dots bouncing in sequence — the loading
//  indicator inside the "please wait" login button. Styling
//  and the bounce animation live in
//  BouncingDotsLoader.module.css (the 2nd and 3rd dots
//  compose the first with a delay).
//
//  Used by:
//    - Login — the login form's submit button while the
//      request is running
// -----------------------------------------------------------

import styles from './BouncingDotsLoader.module.css';


export default function BouncingDotsLoader() {
  return (
    <div className={styles.bouncingLoader}>
      <div className={styles.bouncingLoaderDiv}></div>
      <div className={styles.bouncingLoaderDiv2}></div>
      <div className={styles.bouncingLoaderDiv3}></div>
    </div>
  );
}
