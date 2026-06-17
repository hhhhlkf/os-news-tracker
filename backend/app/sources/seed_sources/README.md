# Seed source probe config

`api` sources can be made queryable by the seed-source smoke probe without
writing Python code. Add an `api_config.probe` block to the YAML source.

Supported modes:

## JSON list

Use this for endpoints that return a JSON object containing a list.

```yaml
- name: Example Releases
  type: api
  url: https://example.com/releases.json
  adapter: example_releases
  api_config:
    probe:
      mode: json_list
      query:
        limit: 20
      items_path: items
      fields:
        title_template: "{id}: {title}"
        url_template: "https://example.com/releases/{id}"
        content:
          - summary
          - description
        published_at: published
```

Notes:

- `query` is appended to the request URL unless the URL already provides that
  key.
- `items_path` is dot-separated, for example `data.items`.
- `title_template` and `url_template` can use fields from each JSON item.
- `content` may be one field or a list of fallback fields.

## HTML table

Use this for simple index/table pages.

```yaml
- name: Example HTML Index
  type: api
  url: https://example.com/index/
  adapter: example_index
  api_config:
    probe:
      mode: html_table
      fields:
        title_template: "Example {cell[1]}"
        url_from_link: 1
        strip_cell_suffix: "/"
        exclude_href_contains:
          - "?"
        include_href_suffix: ".json"
        content_template: "kind={cell[0]}; modified={cell[2]}"
        published_at_cell: 2
```

Notes:

- `{cell[0]}`, `{cell[1]}` refer to table cells in the row.
- `url_from_link` selects the cell containing the link.
- `exclude_href_contains` skips sort/navigation links.
- `include_href_suffix` keeps only links with a specific suffix.

## Text

Use this for a README or single text document.

```yaml
- name: Example Repository
  type: api
  url: https://github.com/example/project
  adapter: example_repo
  api_config:
    probe:
      mode: text
      url: https://raw.githubusercontent.com/example/project/main/README.md
```

The probe uses the first Markdown H1 as the title, falling back to the source
name.
