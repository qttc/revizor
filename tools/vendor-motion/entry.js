// Только то, что зовут слой и сайт. Пишет globalThis.Motion: обёртка overlay_bundle забирает его в __revizorMotion.
import { animate, inView, scroll, stagger } from "motion";
globalThis.Motion = { animate, inView, scroll, stagger };
