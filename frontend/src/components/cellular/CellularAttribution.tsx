import { Fragment } from "react";
import "./CellularFrame.css";

interface AttributionEntry {
  source: string;
  licence: string;
  text: string;
}

interface Props {
  attribution: AttributionEntry[];
  activeSources?: readonly string[];
}

const NAMES: Record<string, string> = {
  opencellid: "OpenCelliD",
  ised: "ISED Canada",
  acma: "ACMA (Australia)",
  fcc_uls: "FCC ULS (United States)",
};

const LINKS: Record<string, { label: string; href: string }[]> = {
  opencellid: [
    { label: "OpenCelliD", href: "https://opencellid.org" },
    { label: "CC BY-SA 4.0", href: "https://creativecommons.org/licenses/by-sa/4.0/" },
  ],
  ised: [
    {
      label: "Open Government Licence - Canada",
      href: "https://open.canada.ca/en/open-government-licence-canada",
    },
  ],
  acma: [{ label: "ACMA", href: "https://www.acma.gov.au" }],
  fcc_uls: [{ label: "FCC ULS", href: "https://www.fcc.gov/uls" }],
};

export default function CellularAttribution({ attribution, activeSources }: Props) {
  if (attribution.length === 0) return null;

  return (
    <section className="card cell-attribution" aria-labelledby="cell-attribution-title">
      <h2 id="cell-attribution-title">Cell data sources</h2>
      <p className="cell-attribution-lede">
        Cell sites on this page come from these open data sources.
      </p>
      <ul className="cell-attribution-list">
        {attribution.map(({ source, licence, text }) => {
          const links = LINKS[source];
          return (
            <li key={source} className="cell-attribution-item" data-source={source}>
              <div className="cell-attribution-head">
                <span className="cell-attribution-name">{NAMES[source] ?? source}</span>
                <span className="cell-attribution-licence mono">{licence}</span>
                {activeSources?.includes(source) && (
                  <span className="cell-attribution-active">In these results</span>
                )}
              </div>
              <p className="cell-attribution-text">{text}</p>
              {links && (
                <p className="cell-attribution-links">
                  {links.map((link, i) => (
                    <Fragment key={link.href}>
                      {i > 0 && (
                        <span className="cell-attribution-sep" aria-hidden="true">
                          {" · "}
                        </span>
                      )}
                      <a href={link.href} target="_blank" rel="noopener noreferrer">
                        {link.label}
                      </a>
                    </Fragment>
                  ))}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
