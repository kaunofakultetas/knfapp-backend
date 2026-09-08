// -----------------------------------------------------------
//  [*] Login page — Particles background
//
//  The animated dots-and-lines background of the login page
//  (tsparticles, slim build). Covers the whole viewport
//  behind the page content (fullScreen with zIndex -1);
//  particles slowly drift, link when close, and are pushed
//  away by the cursor.
//
//  Particle count scales with the viewport width so wide
//  screens don't look empty and phones aren't overcrowded.
// -----------------------------------------------------------

import Particles from "react-tsparticles";
import { loadSlim } from "tsparticles-slim";
import { useCallback, useState, useEffect } from "react";







// -----------------------------------------------------------
// ParticlesComponent (default export)
// -----------------------------------------------------------
//
// Used by:
//   - Login — fullscreen background behind the login card
// -----------------------------------------------------------

export default function ParticlesComponent(props) {

  // 0 on the first render; set from the viewport width after
  // mount (~1 particle per 10px)
  const [particleNumber, setParticleNumber] = useState(0);

  useEffect(() => {
    setParticleNumber(window.innerWidth / 10);
  }, []);


  const options = {
    // Cover the viewport, behind everything else
    fullScreen: {
      enable: true,
      zIndex: -1,
    },
    fpsLimit: 30,   // background effect — no need for 60fps
    detectRetina: true,

    interactivity: {
      events: {
        onClick: {
          enable: false,   // click-to-spawn off (mode kept for easy re-enabling)
          mode: "push",
        },
        onHover: {
          enable: true,
          mode: "repulse",   // particles flee the cursor
        },
      },
      modes: {
        push: {
          quantity: 10,
        },
        repulse: {
          distance: 100,
        },
      },
    },

    particles: {
      // Lines between particles closer than 150px
      links: {
        enable: true,
        distance: 150,
        opacity: 0.5
      },
      move: {
        enable: true,
        speed: { min: 0.01, max: 1.0 },
      },
      // Kept faint — it's a backdrop, not the main attraction
      opacity: {
        value: { min: 0.0, max: 0.2 },
      },
      size: {
        value: { min: 1, max: 3 },
      },
      number: {
        value: particleNumber,
      },
    },
  }


  // Load the slim engine (smaller bundle than the full tsparticles)
  const particlesInit = useCallback(async options => {
    await loadSlim(options);
  }, []);

  return <Particles id={props.id} init={particlesInit} options={options} />;
}
