import "./cellular.css";
import { bandGroup, bandLabel, bandNumber, bandTitle } from "./bands";

interface Props {
  band: string;
  /** Omitted for a chip that names the band alone. */
  radio?: string | null;
  size?: "md" | "sm";
}

/**
 * Band as a chip coloured by its group (low, mid, upper, C-band), with the
 * radio in front when the source records one. NR cells carry an outline so
 * 5G reads apart from LTE on the same band.
 */
export default function BandChip({ band, radio = null, size = "md" }: Props) {
  const classes = ["chip", "cell-band-chip", `cell-band-${bandGroup(band)}`];
  if (radio === "NR") classes.push("is-nr");
  if (size === "sm") classes.push("cell-band-chip-sm");
  return (
    <span className={classes.join(" ")} title={bandTitle(band)}>
      {radio ? bandLabel(radio, band) : bandNumber(band)}
    </span>
  );
}
