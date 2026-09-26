// Both mirror parse_user_frequencies in services/tower_ranking.py, which keeps
// at most ten values and only those in 0 < v < 10000. Anything else is dropped
// there without a word, so the form declines to offer it in the first place.
// The server's upper bound is exclusive and the input's `max` is inclusive, so
// exactly 10000 passes the browser and is dropped below; no illuminator sits
// there.
//
// Shared by the search form and the share-link reader, so a frequency the form
// would not submit is also one a link cannot smuggle in.
export const MAX_FREQUENCIES = 10;
export const MAX_FREQUENCY_MHZ = 10000;

/** One entered frequency as the search will send it, or null if it would be dropped. */
export function parseFrequency(raw: string): number | null {
  const f = parseFloat(raw);
  return !isNaN(f) && f > 0 && f < MAX_FREQUENCY_MHZ ? f : null;
}
