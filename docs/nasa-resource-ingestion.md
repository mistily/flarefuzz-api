# NASA PSI resource ingestion

NASA PSI metadata may return a JSON payload or a direct signed S3 URL. A signed
URL must be fetched before columns are inspected. S3 may instead return
`Content-Type: text/plain` containing:

- newline-delimited absolute links,
- relative file names or relative paths,
- Markdown or HTML links,
- a plain-text CSV/TSV table, or
- JSON despite the `text/plain` header.

Text/plain responses are classified by their contents. Relative names are
resolved against the text response URL. Absolute links and JSON `base_url` /
`file_name` combinations are also resolved and fetched. Redirects are disabled;
only HTTPS NASA hosts and `psi-repo-prod.s3.amazonaws.com` are allowed.

Supported resolved data categories are:

- `diameter_time_series` for diameter versus elapsed time,
- `extinction_diameter_comparison` for experimental versus simulated diameters.

A response for `https://psi-repo-prod.s3.amazonaws.com/psi-data/PSI-117/...`
should be treated as a resource response, not as column-bearing CSV. The path
may contain `...` as a placeholder only; it must be replaced by the actual
signed or public resource path before fetching.
