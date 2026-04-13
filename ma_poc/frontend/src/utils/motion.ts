export const fadeSlideUp = { initial: { opacity: 0, y: 8 }, animate: { opacity: 1, y: 0 }, exit: { opacity: 0, y: -8 }, transition: { duration: 0.2, ease: 'easeOut' } };
export const staggerChildren = { animate: { transition: { staggerChildren: 0.04 } } };
export const cardHover = { whileHover: { y: -2, transition: { duration: 0.15 } } };
