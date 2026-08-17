# STAC Overview

There are three component specifications that together make up the core SpatioTemporal Asset Catalog specification.
Each can be used alone, but they work best in concert with one another. The [STAC API specification](https://github.com/radiantearth/stac-api-spec) 
builds on top of that core, but is out of scope for this overview. An [Item](item-spec/item-spec.md) represents a 
single [spatiotemporal asset](#what-is-a-spatiotemporal-asset) as [GeoJSON](https://geojson.org/) so it can be searched. 
The [Catalog](catalog-spec/catalog-spec.md) specification provides structural elements, to group Items
and [Collections](collection-spec/collection-spec.md). Collections *are* catalogs, that add more required metadata and 
describe a group of related Items. For more on the differences see the [section below](#catalogs-vs-collections).

A [UML diagram](https://en.wikipedia.org/wiki/Unified_Modeling_Language) of the [STAC model](STAC-UML.pdf) is also 
provided to help with navigating the specification. 

## Foundations

STAC is built on top of many great standards and practices. Every part of STAC is 
[JSON](https://www.json.org/json-en.html), and [GeoJSON](https://geojson.org/) provides the core geometry fields 
and [features](https://en.wikipedia.org/wiki/Simple_Features) definition. All fields are described in the 
specifications, and the acceptable values are defined with [JSON Schema](https://json-schema.org/). The released
JSON Schemas provide the core testing definitions, and are used in an array of validation tools. We also rely
on [RFC 8288 (Web Linking)](https://tools.ietf.org/rfc/rfc8288.txt) to express relationships between resources,
and IANA [Media Types](https://en.wikipedia.org/wiki/Media_type) to describe file formats and format contents.
The [OGC API - Features](https://ogcapi.ogc.org/features/) standard is a final core building block. The STAC
Collection extends the [Collection](http://docs.opengeospatial.org/is/17-069r3/17-069r3.html#_collection_)
JSON defined in OGC API - Features (and the full API definition is the foundation for the STAC API specification).

The STAC specifications are written to be understandable without needing a full background in these. But if you 
want to get deep into STAC tool implementation and are not familiar with any of the standards mentioned above it is 
recommended to read up on them. STAC development is guided by set of core philosophical tenets, like 
building small reusable parts that are loosely coupled, focusing on developers, and more - see our the 
[principles](principles.md) document to learn more.

*Note: Setting a field in JSON to `null` is not equivalent to a field not appearing in STAC, as JSON Schema tools treat
them differently. STAC defines `null` explicitly for some fields, where it has a particular meaning. So `null` should 
not be used unless the STAC spec defines its use - instead the field should be left out entirely.* 

## Item Overview

Fundamental to any SpatioTemporal Asset Catalog, an [Item](item-spec/item-spec.md) object represents a unit of
data and metadata, typically representing a single scene of data at one place and time.   A STAC Item is a 
[GeoJSON](http://geojson.org/) [Feature](https://tools.ietf.org/html/rfc7946#section-3.2)
and can be easily read by any modern GIS or geospatial library, and it describes a 
[SpatioTemporal Asset](#what-is-a-spatiotemporal-asset). 
The STAC Item JSON specification uses the GeoJSON geometry to describe the location of the asset, and 
then includes additional information:

- the time the asset represents;
- a thumbnail for quick browsing;
- asset links, to enable direct download or streaming access of the asset;
- relationship links, allowing users to traverse other related resources and STAC Items.

A STAC Item can contain additional fields and JSON structures to communicate more information about the
asset, so it can be easily searched. STAC provides a core set of 
[Common Metadata](commons/common-metadata.md)
and there is a wider community working on a variety of [STAC Extensions](extensions/) that provide shared metadata for 
more specific domains. Both aim to describe data with well known, well
defined terms to enable consistent publishing and better search. For more recommendations on selecting fields
for an Item see [this section](best-practices.md#field-selection-and-metadata-linking) of the best practices document.

### What is a SpatioTemporal Asset

A 'spatiotemporal asset' is any file that represents information about the earth captured in a certain 
space and time. Examples include Imagery (from satellites, planes and drones), SAR, Point Clouds (from
LiDAR, Structure from Motion, etc), Data Cubes, Full Motion Video, and data derived from any of those.
The key is that the GeoJSON is not the actual 'thing', but instead references files and serves as an
index to the 'assets'. It is [not recommended](best-practices.md#representing-vector-layers-in-stac) 
to use STAC to refer to traditional vector data layers (shapefile, geopackage) as assets, as they
don't quite fit conceptually. 

## Catalogs vs Collections

Before we go deep into the Catalogs and Collections, it is worth explaining the relationship 
between the two and when you might want to use one or the other. 

A Catalog is a very simple construct - it just provides links to Items or to other Catalogs. 
The closest analog is a folder in a file structure, it is the container for Items, but it can 
also hold other containers (folders / catalogs). 

The Collection entity shares most fields with the Catalog entity but has a number of additional fields:
license, extent (spatial and temporal), providers, keywords and summaries. Every Item in a Collection links
back to their Collection, so clients can easily find fields like the license. Thus every Item implicitly 
shares the fields described in their parent Collection. Collection entities can be used just like Catalog 
entities to provide structure, as they provide all the same options for linking and organizing.

But what *should* go in a Collection, versus just in a Catalog?  A Collection will generally consist of
a set of assets that are defined with the same properties and share higher level metadata. In the 
satellite world these would typically all come from the same sensor or constellation. It corresponds
directly to what others call a "dataset series" (ESA, ISO 19115), "collection" (CNES, NASA), and 
"dataset" (JAXA, DCAT). So if all your Items have the same properties, they probably belong in 
the same Collection. But the construct is deliberately flexible, as there may be good reasons
to break the recommendation.

Catalogs in turn are used for two main things:

- Split overly large collections into groups
- Group collections into a catalog of Collections (e.g. as entry point for navigation to several Collections).

The first case allows users to browse down into the Items of large collections. A collection like
Landsat usually would start with path and row Catalogs to group by geography, and then year, 
month and day groups to enable deeper grouping. [Dynamic catalogs](best-practices.md#dynamic-catalogs) can
provide multiple grouping paths, serving as a sort of faceted search.

The second case is used when one wants to represent diverse data in a single place. If an organization
has an internal catalog with Landsat 8, Sentinel 2, NAIP data and several commercial imagery providers
then they'd have a root Catalog that would link to a number of different Collections. 

So in conclusion it's best to use Collections for what you want users to find as the starting point, and then
Catalogs are just for structuring and grouping the data. Future work includes a mechanism to actually
search Collection-level data, hopefully in concert with other specifications.

## Catalog Overview

*NOTE: The below examples all say Catalog, but those can all be Collections as well, as it has all the fields necessary to 
serve as a Catalog*

There are two required element types of a Catalog: Catalog and Item. A STAC Catalog
points to [STAC Items](item-spec/README.md), or to other STAC catalogs. It provides a simple
linking structure that can be used recursively so that many Items can be included in 
a single Catalog, organized however the implementor desires. 

STAC makes no formal distinction between a "root" Catalog and the "child" Catalogs. A root Catalog
is simply the top-most Catalog or Collection -- it has no parent. A nested catalog structure is useful (and
recommended) for breaking up massive numbers of catalog Items into logical groupings. For example,
it might make sense to organize a catalog by date (year, month, day), or geography (continent,
country, state/prov). See the [Catalog Layout](best-practices.md#catalog-layout) best practices
section for more.

A simple STAC structure might look like this:

- catalog (root)
  - catalog
    - catalog
      - item
        - asset
      - item
        - asset
    - item
      - asset
      - asset

This example might be considered a somewhat "typical" structure. However, Catalogs and Items can
describe a number of different relationships. The following shows various relationships between
catalogs and items:

- `Catalog` -> `Item` (this is a common structure for a catalog to list links to Items)
- `Catalog` -> `Catalog` (this is a common tree structure to group sets of Items. Each catalog in
  this relationship may also include Item links as well as catalog links)

The relationships are all described by a common `links` object structure, making use of
the `rel` field to further describe the relationship. 

There are a few types of catalogs that implementors occasionally refer to. These get defined by the `links` structure.

- A **sub-catalog** is a Catalog that is linked to from another Catalog that is used to better organize data. For example a Landsat collection
  might have sub-catalogs for each Path and Row, so as to create a nice tree structure for users to follow.
- A **root catalog** is a Catalog that only links to sub-catalogs. These are typically entry points for browsing data. Often
  they will contain the [STAC Collection](collection-spec/) definition, but in implementations that publish diverse information it may
  contain sub-catalogs that provide a variety of Collections.
- A **parent catalog** is the Catalog that sits directly above a sub-catalog. Following parent catalog links continuously
  will naturally end up at a root catalog definition.
 
It should be noted that a Catalog does not have to link back to all the other Catalogs that point to it. Thus a published 
root catalog might be a sub-catalog of someone else's structure. The goal is for data providers to publish all the 
information and links they want to, while also encouraging a natural web of information to arise as Catalogs and Items are
linked to across the web.

### Static and Dynamic Catalogs

The Catalog specification is designed so it can be implemented as easily as possible. This can be as simple as
simply putting linked json files on a file server or an object storage service (like [AWS S3](https://aws.amazon.com/s3/)),
or it can be generated on the fly by a live server. The first type of implementation is often called a 'static catalog',
and any catalog that is not just files is called a 'dynamic catalog'. You can read more about the two types along with
recommendations in [this section](best-practices.md#static-and-dynamic-catalogs) of the best practices document, 
along with how to keep a [dynamic catalog in sync](best-practices.md#static-to-dynamic-best-practices) with a static one.

### Catalog Best Practices

In addition to information about different catalog types, the [best practices document](best-practices.md) has
a number of suggestions on how to organize and implement good catalogs. The [catalog specification](catalog-spec/catalog-spec.md)
is designed for maximum flexibility, so none of these are required, but they provide guidance for implementors who
want to follow what most of the STAC community is doing.

- [Catalog Layout](best-practices.md#catalog-layout) is likely the most important section, as following its 
recommendations will enable catalogs to work better with client tooling that optimizes for known layouts.
- [Use of Links](best-practices.md#use-of-links) articulates practices for making catalogs that are portable (with
relative links through out) and ones that are published in stable locations (with absolute self links).
- [Versioning for Catalogs](best-practices.md#versioning-for-catalogs) explains how to use STAC's structure to
keep a history of changes made to Items and catalogs.
- [STAC on the Web](best-practices.md#stac-on-the-web) explains how catalogs should have html versions for 
each Item and Catalog, as well as ways to achieve that.

## Collection Overview

A STAC Collection includes the core fields of the Catalog entity and also provides additional metadata to describe 
the set of Items it contains. The required fields are fairly 
minimal - it includes the 4 required Catalog fields (id, description, stac_version and links), and adds license 
and extents. But there are a number of other common fields defined in the spec, and more common fields are also 
defined in [STAC extensions](extensions/). These serve as basic metadata, and ideally Collections also link to 
fuller metadata (ISO 19115, etc) when it is available.

As Collections contain all of Catalogs' core fields, they can be used just as flexibly. They can have both parent Catalogs and Collections
as well as child Items, Catalogs and Collections. Items are strongly recommended to have a link to the Collection
they are a part of. Items can only belong to one Collection, so if an Item is in a Collection that is the child of 
another Collection, then it must pick which one to refer to. Generally the 'closer' Collection, the more specific
one, should be the one linked to.

The Collection specification is used standalone quite easily - it is used to describe an aggregation of data, 
and doesn't require links down to sub-catalogs and Items. This is most often used when the software
does operations at the layer / coverage level, letting users manipulate a whole collection of assets at once. They often
have an optimized internal format that doesn't make sense to expose as Items. [OpenEO](https://openeo.org/) and 
[Google Earth Engine](https://earthengine.google.com/) are two examples that only use STAC collections, and
both would be hard-pressed to expose individual Items due to their architectures. For others implementing STAC
Collections can also be a nice way to start and achieve some level of interoperability. 
# STAC Catalog Specification <!-- omit in toc -->

- [Catalog fields](#catalog-fields)
  - [stac\_version](#stac_version)
  - [stac\_extensions](#stac_extensions)
  - [links](#links)
    - [Relation types](#relation-types)
- [Media Type for STAC Catalogs](#media-type-for-stac-catalogs)
- [Extensions](#extensions)

This document explains the structure and content of a STAC **Catalog** object. A STAC Catalog object
represents a logical group of other Catalog,
[Collection](../collection-spec/collection-spec.md), and [Item](../item-spec/item-spec.md) objects.
These Items can be linked to directly from a Catalog, or the Catalog can link to other Catalogs (often called
sub-catalogs) that contain links to Collections and Items. The division of sub-catalogs is up to the implementor,
but is generally done to aid the ease of online browsing by people.

A Catalog object will typically be the entry point into a STAC catalog. Their
purpose is discovery: to be browsed by people or be crawled
by clients to build a searchable index.  

Any JSON object that contains all the required fields is a valid STAC Catalog object.

- [Examples](../examples/)
  - See an example [catalog.json](../examples/catalog.json). The [collection.json](../examples/collection.json) is also a valid
  Catalog file, demonstrating linking to items (it is also a Collection, so has additional fields)
- [JSON Schema](json-schema/catalog.json)

The [Catalog section of the Overview](../overview.md#catalog-overview) document provides background information on
the structure of Catalogs as well as links to best practices. This specification lays out the requirements
and fields to be compliant.

This Catalog specification primarily defines a structure for information to be discoverable. Any use
that is publishing a set of related spatiotemporal assets is strongly recommended to also use the
STAC Collection specification to provide additional information about the set of Items
contained in a Catalog, in order to give contextual information to aid in discovery.
STAC Collections all have the same fields as STAC Catalogs, but with different allowed
values for `type` and `stac_extensions`.

## Catalog fields

| Element         | Type                    | Description                                                                                                                                                            |
| --------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| type            | string                  | **REQUIRED.** Set to `Catalog` if this Catalog only implements the Catalog spec.                                                                                       |
| stac_version    | string                  | **REQUIRED.** The STAC version the Catalog implements.                                                                                                                 |
| stac_extensions | \[string]               | A list of extension identifiers the Catalog implements.                                                                                                                |
| id              | string                  | **REQUIRED.** Identifier for the Catalog.                                                                                                                              |
| title           | string                  | A short descriptive one-line title for the Catalog.                                                                                                                    |
| description     | string                  | **REQUIRED.** Detailed multi-line description to fully explain the Catalog. [CommonMark 0.29](http://commonmark.org/) syntax MAY be used for rich text representation. |
| links           | [[Link Object](#links)] | **REQUIRED.** A list of references to other documents.                                                                                                                 |

### stac_version

In general, STAC versions can be mixed, but please keep the [recommended best practices](../best-practices.md#mixing-stac-versions) in mind.

### stac_extensions

A list of extensions the Catalog implements.
The list consists of URLs to JSON Schema files that can be used for validation.
This list must only contain extensions that extend the Catalog specification itself,
see the 'Scope' for each of the extensions.
This must **not** declare the extensions that are only implemented in child Collection objects or child Item objects.

### links

Each link in the `links` array must be a [Link Object](../commons/links.md#link-object).

#### Relation types

All the [common relation types](../commons/links.md#relation-types) can be used in catalog.
A `self` and a `root` links are STRONGLY RECOMMENDED.
Non-root Catalogs SHOULD include a `parent` link to their parent.

> \[!NOTE] A link to at least one `item` or `child` (Catalog or Collection) is **RECOMMENDED**, but empty catalogs are
> allowed if there is an intent to populate it or its children were removed.

## Media Type for STAC Catalogs

A STAC Catalog is a JSON file ([RFC 8259](https://tools.ietf.org/html/rfc8259)), and thus should use the
[`application/json`](https://tools.ietf.org/html/rfc8259#section-11) as the
[Media Type](https://en.wikipedia.org/wiki/Media_type) (previously known as the MIME Type).

## Extensions

STAC Catalogs are [extensible](../extensions/README.md).
Please refer to the [extensions overview](https://stac-extensions.github.io) to find relevant extensions for STAC Catalogs.
# STAC Collection Specification <!-- omit in toc -->

- [Overview](#overview)
- [Collection fields](#collection-fields)
  - [stac\_version](#stac_version)
  - [stac\_extensions](#stac_extensions)
  - [id](#id)
  - [license](#license)
  - [providers](#providers)
    - [Provider Object](#provider-object)
  - [extents](#extents)
    - [Extent Object](#extent-object)
    - [Spatial Extent Object](#spatial-extent-object)
    - [Temporal Extent Object](#temporal-extent-object)
  - [summaries](#summaries)
    - [Range Object](#range-object)
    - [JSON Schema Object](#json-schema-object)
  - [links](#links)
    - [Relation types](#relation-types)
  - [assets](#assets)
  - [item\_assets](#item_assets)
    - [Item Asset Definition Object](#item-asset-definition-object)
- [Media Type for STAC Collections](#media-type-for-stac-collections)
- [Standalone Collections](#standalone-collections)
- [Extensions](#extensions)

## Overview

The STAC Collection Specification defines a set of common fields to describe a group of Items that share properties and metadata. The
Collection Specification shares all fields with the STAC [Catalog Specification](../catalog-spec/catalog-spec.md) (with different allowed
values for `type` and `stac_extensions`) and adds fields to describe the whole dataset and the included set of Items. Collections
can have both parent Catalogs and Collections and child Items, Catalogs and Collections.

A STAC Collection is represented in JSON format.
Any JSON object that contains all the required fields is a valid STAC Collection and also a valid STAC Catalog.

STAC Collections are compatible with the [Collection](http://docs.opengeospatial.org/is/17-069r3/17-069r3.html#example_4) JSON
specified in [*OGC API - Features*](https://ogcapi.ogc.org/features/), but they are extended with additional fields.  

- [Examples](../examples/):
  - Sentinel 2: A basic standalone example of a [Collection](../examples/collection-only/collection.json) without Items.
  - Simple Example: A [Collection](../examples/collection.json) that links to 3 example Items.
  - Extension Collection: An additional [Collection](../examples/extensions-collection/collection.json), which is used to highlight
  various [extension](../extensions) functionality, but serves as another example.
- [JSON Schema](json-schema/collection.json)

## Collection fields

| Element         | Type                                                                                         | Description                                                                                                                                                               |
| --------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| type            | string                                                                                       | **REQUIRED.** Must be set to `Collection` to be a valid Collection.                                                                                                       |
| stac_version    | string                                                                                       | **REQUIRED.** The STAC version the Collection implements.                                                                                                                 |
| stac_extensions | \[string]                                                                                    | A list of extension identifiers the Collection implements.                                                                                                                |
| id              | string                                                                                       | **REQUIRED.** Identifier for the Collection that is unique across all collections in the root catalog.                                                                    |
| title           | string                                                                                       | A short descriptive one-line title for the Collection.                                                                                                                    |
| description     | string                                                                                       | **REQUIRED.** Detailed multi-line description to fully explain the Collection. [CommonMark 0.29](http://commonmark.org/) syntax MAY be used for rich text representation. |
| keywords        | \[string]                                                                                    | List of keywords describing the Collection.                                                                                                                               |
| license         | string                                                                                       | **REQUIRED** License(s) of the data collection as SPDX License identifier, SPDX License expression, or `other` (see below).                                               |
| providers       | \[[Provider Object](#provider-object)]                                                       | A list of providers, which may include all organizations capturing or processing the data or the hosting provider.                                                        |
| extent          | [Extent Object](#extent-object)                                                              | **REQUIRED.** Spatial and temporal extents.                                                                                                                               |
| summaries       | Map<string, \[\*]\|[Range Object](#range-object)\|[JSON Schema Object](#json-schema-object)> | STRONGLY RECOMMENDED. A map of property summaries, either a set of values, a range of values or a [JSON Schema](https://json-schema.org).                                 |
| links           | \[[Link Object](#links)]                                                                     | **REQUIRED.** A list of references to other documents.                                                                                                                    |
| assets          | Map<string, [Asset Object](#assets)>                                                         | Dictionary of asset objects that can be downloaded, each with a unique key.                                                                                               |
| item_assets     | Map<string, [Item Asset Definition Object](#item-asset-definition-object)>                   | A dictionary of assets that can be found in member Items.                                                                                                                 |

### stac_version

In general, STAC versions can be mixed, but please keep the [recommended best practices](../best-practices.md#mixing-stac-versions) in mind.

### stac_extensions

A list of extensions the Collection implements.
The list consists of URLs to JSON Schema files that can be used for validation.
This list must only contain extensions that extend the Collection specification itself,
see the 'Scope' for each of the extensions.
This must **not** declare the extensions that are only implemented in child Collection objects or child Item objects.

### id

It is important that Collection identifiers are unique across all collections in the corresponding root catalog.
Providers should strive as much as possible to make their Collection ids 'globally' unique, prefixing any common information with a unique string.
This could be the provider's name if it is a fairly unique name, or their name combined with the domain they operate in.

### license

License(s) of the data that the STAC Collection and its children provides.
If possible, license information should be defined at the Collection level.

The license(s) can be provided as:

1. [SPDX License identifier](https://spdx.org/licenses/)
2. [SPDX License expression](https://spdx.github.io/spdx-spec/v2.3/SPDX-license-expressions/)
3. String with the value `other` if the license is not on the SPDX license list.
   The strings `various` and `proprietary` are **deprecated**.

If the license is **not** an SPDX license identifier, links to the license texts SHOULD be added.
The links MUST use the [`license` link relation type](#relation-types).
If there is no public license URL available,
it is RECOMMENDED to supplement the STAC Item with the license text in a separate file and link to this file.
If no link to a license is included and the `license` field is set to `other` (or one of the deprecated values),
the Collection is private, and consumers have not been granted any explicit right to use the data.

### providers

A list of providers, which may include all organizations capturing or processing the data or the hosting provider.
Providers should be listed in chronological order with the most recent provider being the last element of the list.

#### Provider Object

The object provides information about a provider.
A provider is any of the organizations that captures or processes the content of the Collection
and therefore influences the data offered by this Collection.
May also include information about the final storage provider hosting the data.

| Field Name  | Type      | Description                                                                                                                                                                                                                                                            |
| ----------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| name        | string    | **REQUIRED.** The name of the organization or the individual.                                                                                                                                                                                                          |
| description | string    | Multi-line description to add further provider information such as processing details for processors and producers, hosting details for hosts or basic contact information. [CommonMark 0.29](http://commonmark.org/) syntax MAY be used for rich text representation. |
| roles       | \[string] | Roles of the provider. Any of `licensor`, `producer`, `processor` or `host`.                                                                                                                                                                                           |
| url         | string    | Homepage on which the provider describes the dataset and publishes contact information.                                                                                                                                                                                |

**roles**: The provider's role(s) can be one or more of the following elements:

- *licensor*: The organization that is licensing the dataset under the license specified in the Collection's `license` field.
- *producer*: The producer of the data is the provider that initially captured and processed the source data, e.g. ESA for Sentinel-2 data.
- *processor*: A processor is any provider who processed data to a derived product.
- *host*: The host is the actual provider offering the data on their storage.
  There should be no more than one host, specified as last element of the list.

### extents

#### Extent Object

The object describes the spatio-temporal extents of the Collection. Both spatial and temporal extents are required to be specified.

| Element  | Type                                              | Description                                                           |
| -------- | ------------------------------------------------- | --------------------------------------------------------------------- |
| spatial  | [Spatial Extent Object](#spatial-extent-object)   | **REQUIRED.** Potential *spatial extents* covered by the Collection.  |
| temporal | [Temporal Extent Object](#temporal-extent-object) | **REQUIRED.** Potential *temporal extents* covered by the Collection. |

#### Spatial Extent Object

The object describes the spatial extents of the Collection.

| Element | Type         | Description                                                          |
| ------- | ------------ | -------------------------------------------------------------------- |
| bbox    | \[\[number]] | **REQUIRED.** Potential *spatial extents* covered by the Collection. |

**bbox**: Each outer array element can be a separate spatial extent describing the bounding boxes
of the assets represented by this Collection using either 2D or 3D geometries.

The first bounding box always describes the overall spatial extent of the data. All subsequent bounding boxes can be
used to provide a more precise description of the extent and identify clusters of data.
Clients only interested in the overall spatial extent will only need to access the first item in each array.
It is recommended to only use multiple bounding boxes if a union of them would then include
a large uncovered area (e.g. the union of Germany and Chile).
Thus, it doesn't make sense to provide two bounding boxes and the validation will fail in this case.

The length of the inner array must be 2*n where n is the number of dimensions.
The array contains all axes of the southwesterly most extent followed by all axes of the northeasterly most extent specified in
Longitude/Latitude or Longitude/Latitude/Elevation based on [WGS 84](http://www.opengis.net/def/crs/OGC/1.3/CRS84).
When using 3D geometries, the elevation of the southwesterly most extent is the minimum depth/height in meters
and the elevation of the northeasterly most extent is the maximum.

The coordinate reference system of the values is WGS 84 longitude/latitude.
Example that covers the whole Earth: `[[-180.0, -90.0, 180.0, 90.0]]`.
Example that covers the whole earth with a depth of 100 meters to a height of 150 meters: `[[-180.0, -90.0, -100.0, 180.0, 90.0, 150.0]]`.

#### Temporal Extent Object

The object describes the temporal extents of the Collection.

| Element  | Type               | Description                                                           |
| -------- | ------------------ | --------------------------------------------------------------------- |
| interval | \[\[string\|null]] | **REQUIRED.** Potential *temporal extents* covered by the Collection. |

**interval**: Each outer array element can be a separate temporal extent.
The first time interval always describes the overall temporal extent of the data. All subsequent time intervals
can be used to provide a more precise description of the extent and identify clusters of data.
Clients only interested in the overall extent will only need to access the first item in each array.
It is recommended to only use multiple temporal extents if a union of them would then include a large
uncovered time span (e.g. only having data for the years 2000, 2010 and 2020).

Each inner array consists of exactly two elements, either a timestamp or `null`.

Timestamps consist of a date and time in UTC and MUST be formatted according to
[RFC 3339, section 5.6](https://tools.ietf.org/html/rfc3339#section-5.6).
The temporal reference system is the Gregorian calendar.

Open date ranges are supported by setting the start and/or the end time to `null`.
Example for data from the beginning of 2019 until now: `[["2019-01-01T00:00:00Z", null]]`.
It is recommended to provide at least a rough guideline on the temporal extent and thus
it's not recommended to set both start and end time to `null`. Nevertheless, this is possible
if there's a strong use case for an open date range to both sides.

### summaries

Collections are *strongly recommended* to provide summaries of the values of fields that they can expect from the `properties`
of STAC Items contained in this Collection. This enables users to get a good sense of what the ranges and potential values of
different fields in the Collection are, without having to inspect a number of Items (or crawl them exhaustively to get a definitive answer).
Summaries are often used to give users a sense of the data in [Standalone Collections](#standalone-collections),
describing the potential values even when it can't be accessed as Items. They also give clients enough information to
build tailored user interfaces for querying the data, by presenting the potential values that are available.
 Fields selected to be included in summaries should capture all the potential values of the
 field that appear in every Item underneath the collection, including in any nested sub-Catalogs.

A summary for a field can be specified in three ways:

1. A set of all distinct values in an array: The set of values must contain at least one element and it is strongly recommended to list all values.
   If the field summarizes an array (e.g. [`instruments`](../commons/common-metadata.md#instrument)),
   the field's array elements of each Item must be merged to a single array with unique elements.
2. A Range in a [Range Object](#range-object): Statistics by default only specify the range (minimum and maximum values),
   but can optionally be accompanied by additional statistical values.
   The range specified by the `minimum` and `maximum` properties can specify the potential range of values,
   but it is recommended to be as precise as possible.
3. Extensible JSON Schema definitions for fine-grained information, see the [JSON Schema Object](#json-schema-object)
   section for more.

All values must follow the schema of the property field they summarize, unless the field is an array as described in (1) above.
So the values in the array or the values given for `minimum` and `maximum` must comply to the original data type
and any further restrictions that apply for the property they summarize.
For example, the `minimum` for `gsd` can't be lower than zero and the summaries for `platform` and `instruments`
must each be an array of strings (or alternatively minimum and maximum values, but that's not very meaningful).

It is recommended to list as many properties as reasonable so that consumers get a full overview about the properties included in the Items.
Nevertheless, it is not very useful to list all potential `title` values of the Items.
Also, a range for the `datetime` property may be better suited to be included in the STAC Collection's `extent` field.
In general, properties that are covered by the Collection specification should not be repeated in the summaries.

See the [examples folder](../examples) for Collections with summaries to get a sense of how to use them.

#### Range Object

For summaries that would normally consist of a lot of continuous values, statistics can be added instead.
By default, only ranges with a minimum and a maximum value can be specified.
Ranges can be specified for [ordinal](https://en.wikipedia.org/wiki/Level_of_measurement#Ordinal_scale) values only,
which means they need to have a rank order.
Therefore, ranges can only be specified for numbers and some special types of strings. Examples: grades (A to F), dates or times.
Implementors are free to add other derived statistical values to the object, for example `mean` or `stddev`.

| Field Name | Type           | Description                  |
| ---------- | -------------- | ---------------------------- |
| minimum    | number\|string | **REQUIRED.** Minimum value. |
| maximum    | number\|string | **REQUIRED.** Maximum value. |

#### JSON Schema Object

For a full understanding of the summarized field, a JSON Schema can be added for each summarized field.
This allows very fine-grained information for each field and each value as JSON Schema is also extensible.
Each schema must be valid against all corresponding values available for the property in the sub-Items.
Empty schemas are not allowed.

JSON Schema draft-07 is the default JSON Schema version, which aligns with the JSON Schemas provided by STAC.
It is allowed to use other versions of JSON Schema if the version is explicitly expressed in the JSON Schema `$schema` keyword,
but tooling may not support JSON Schema versions other than `draft-07`.

For an introduction to JSON Schema, see "[Learn JSON Schema](https://json-schema.org/learn/)".

### links

This object is described in the [Links](../commons/links.md) document.

#### Relation types

All the [common relation types](../commons/links.md#relation-types) can be used in Collection.
A `self` and a `root` links are STRONGLY RECOMMENDED.
Non-root Collections SHOULD include a `parent` link to their parent.

> \[!NOTE] The STAC Catalog specification requires a link to at least one `item` or `child` Catalog.
> This is *not* a requirement for Collections, but *recommended*. In contrast to Catalogs,
> it is **REQUIRED** that Items linked from a Collection MUST refer back to its Collection
> with a link with the [`collection` relation type](../item-spec/item-spec.md#relation-types).

### assets

Collection Assets provides an optional mechanism to expose assets that don't make sense at the Item level.
The property `assets` is a dictionary of [Asset Objects](../commons/assets.md#asset-object), each with a unique key.
Each asset refers to data associated with the Collection that can be downloaded or streamed.
This construct is further detailed in the [Assets](../commons/assets.md) document.

There are a few guidelines for using the asset construct at the Collection level:

- Collection-level assets SHOULD NOT list any files also available in Items.
- If possible, item-level assets are always the preferable way to expose assets.

Collection-level assets can be useful in some scenarios, for example:

1. Exposing additional data that applies Collection-wide and you don't want to expose it in each Item.
   This can be Collection-level metadata or a thumbnail for visualization purposes.
2. Individual Items can't properly be distinguished for some data structures,
   e.g. [Zarr](https://zarr.readthedocs.io/) as it's a data structure not contained in single files.
3. Exposing assets for
   "[Standalone Collections](https://github.com/radiantearth/stac-spec/blob/master/collection-spec/collection-spec.md#standalone-collections)".

Often, it is possible to model data and assets with either a Collection or an Item.
In those scenarios we *recommend* to use Items as much as possible, as they are designed for assets.
Using Collection-level assets should only be used if there is no other option.

### item_assets

This serves two purposes:

1. Provide a human-readable definition of assets available in **any** Items
   belonging to this Collection so that the user can determine the key(s)
   of assets they are interested in.
2. Provide a way to programmatically determine what assets are available
   in **any** member Item. Otherwise a random Item needs to be examined to
   determine assets available, but a random Item may not be representative of the set.

An Item Asset Object defined at the Collection level is nearly the same as the
[Asset Object in Items](../commons/assets.md#asset-object), except for two differences.
The `href` field is not required, because Item Asset Definitions don't point to any data by themselves, but at least two other fields must be present.

#### Item Asset Definition Object

An item asset is an object that contains details about the datafiles that will be included in member Items.
Assets included at the Collection level do not imply that all assets are available from all Items.
However, it is recommended that the Asset Definition is a complete set of **all** assets that may be available from **any** member Items.
So this should be the union of the available assets, not just the intersection of the available assets.

| Field Name  | Type      | Description                                                                                                                                                                                  |
| ----------- | --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| title       | string    | The displayed title for clients and users.                                                                                                                                                   |
| description | string    | A description of the Asset providing additional details, such as how it was processed or created. [CommonMark 0.29](http://commonmark.org/) syntax MAY be used for rich text representation. |
| type        | string    | [Media type](../commons/assets.md#media-types) of the asset.                                                                                                                                 |
| roles       | \[string] | The [semantic roles](../commons/assets.md#roles) of the asset, similar to the use of `rel` in links.                                                                                         |

Other custom fields, or fields from other extensions may also be included in the Asset object.

Any property that exists for a Collection-level asset object must also exist in the corresponding assets object in
each Item. If a collection's asset object contains properties that are not explicitly stated in the Item's asset
object then that property does not apply to the item's asset. Item asset objects at the Collection-level can
describe any of the properties of an asset, but those assets properties and values must also reside in the item's
asset object. To consolidate item-level asset object properties in an API setting, consider storing the STAC Item
objects without the larger properties internally as 'invalid' STAC items, and merge in the desired properties at
serving time from the Collection-level.

At least two fields (e.g. `title` and `type`) are required to be provided, in order for it to adequately describe Item assets.
The two fields must not necessarily be taken from the list above and may include any custom field.

## Media Type for STAC Collections

A STAC Collection is a JSON file ([RFC 8259](https://tools.ietf.org/html/rfc8259)), and thus should use the
[`application/json`](https://tools.ietf.org/html/rfc8259#section-11) as the [Media Type](https://en.wikipedia.org/wiki/Media_type)
(previously known as the MIME Type).

## Standalone Collections

STAC Collections which don't link to any Item are called **standalone Collections**.
To describe them with more fields than the Collection fields has to offer,
it is allowed to re-use the metadata fields defined by extensions for Items in the `summaries` field.
This makes much sense for fields such as `platform` or `proj:code`, which are often the same for a whole Collection,
but doesn't make much sense for `eo:cloud_cover`, which usually varies heavily across a Collection.
The data provider is free to decide, which fields are reasonable to be used.

## Extensions

STAC Collections are [extensible](../extensions/README.md).
Please refer to the [extensions overview](https://stac-extensions.github.io) to find relevant extensions for STAC Collections.
# STAC Item Specification <!-- omit in toc -->

- [Overview](#overview)
- [Item fields](#item-fields)
  - [stac\_version](#stac_version)
  - [stac\_extensions](#stac_extensions)
  - [id](#id)
  - [geometry](#geometry)
  - [bbox](#bbox)
  - [collection](#collection)
  - [properties](#properties)
    - [Properties Object](#properties-object)
    - [datetime](#datetime)
    - [Additional Fields](#additional-fields)
  - [Links](#links)
    - [Relation types](#relation-types)
    - [Collections](#collections)
  - [Assets](#assets)
- [Media Type for STAC Item](#media-type-for-stac-item)
- [Extensions](#extensions)

## Overview

This document explains the structure and content of a SpatioTemporal Asset Catalog (STAC) Item. An **Item** is a
[GeoJSON](http://geojson.org/) [Feature](https://tools.ietf.org/html/rfc7946#section-3.2) augmented with
[foreign members](https://tools.ietf.org/html/rfc7946#section-6) relevant to a STAC object.
These include fields that identify the time range and assets of the Item. An Item is the core
object in a STAC Catalog, containing the core metadata that enables any client to search or crawl
online catalogs of spatial 'assets' (e.g., satellite imagery, derived data, DEMs).

The same Item definition is used in both [STAC Catalogs](../catalog-spec/README.md) and
the [Item-related API endpoints](https://github.com/radiantearth/stac-api-spec/blob/master/api-spec.md#ogc-api---features-endpoints).
Catalogs are simply sets of Items that are linked online, generally served by simple web servers
and used for crawling data. The search endpoint enables dynamic queries, for example selecting all
Items in Hawaii on June 3, 2015, but the results they return are FeatureCollections of Items.

Items are represented in JSON format and are very flexible. Any JSON object that contains all the
required fields is a valid STAC Item.

- Examples:
  - See the [minimal example](../examples/simple-item.json),
    as well as a [more fleshed example](../examples/core-item.json) that contains a number of current best practices.
  - Real world [implementations](https://stacindex.org/catalogs) are also available.
- [JSON Schema](json-schema/item.json)

## Item fields

This object describes a STAC Item. The fields `id`, `type`, `bbox`, `geometry` and `properties` are
inherited from GeoJSON.

| Field Name      | Type                                    | Description                                                                                                                                                                                                                                                                                               |
| --------------- | --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| type            | string                                  | **REQUIRED.** Type of the GeoJSON Object. MUST be set to `Feature`.                                                                                                                                                                                                                                       |
| stac_version    | string                                  | **REQUIRED.** The STAC version the Item implements.                                                                                                                                                                                                                                                       |
| stac_extensions | \[string]                               | A list of extensions the Item implements.                                                                                                                                                                                                                                                                 |
| id              | string                                  | **REQUIRED.** Provider identifier. The ID should be unique within the [Collection](../collection-spec/collection-spec.md) that contains the Item.                                                                                                                                                         |
| geometry        | GeoJSON Geometry Object \| null         | **REQUIRED.** Defines the full footprint of the asset represented by this item, formatted according to RFC 7946, [section 3.1](https://tools.ietf.org/html/rfc7946#section-3.1) if a geometry is provided or [section 3.2](https://tools.ietf.org/html/rfc7946#section-3.2) if *no* geometry is provided. |
| bbox            | \[number]                               | **REQUIRED if `geometry` is not `null`, prohibited if `geometry` is `null`.** Bounding Box of the asset represented by this Item, formatted according to [RFC 7946, section 5](https://tools.ietf.org/html/rfc7946#section-5).                                                                            |
| properties      | [Properties Object](#properties-object) | **REQUIRED.** A dictionary of additional metadata for the Item.                                                                                                                                                                                                                                           |
| links           | \[[Link Object](#links)]                | **REQUIRED.** List of link objects to resources and related URLs. See the [best practices](../best-practices.md#use-of-links) for details on when the use `self` links is strongly recommended.                                                                                                           |
| assets          | Map<string, [Asset Object](#assets)>    | **REQUIRED.** Dictionary of asset objects that can be downloaded, each with a unique key.                                                                                                                                                                                                                 |
| collection      | string                                  | The `id` of the STAC Collection this Item references to. This field is **required** if a link with a `collection` relation type is present and is **not allowed** otherwise.                                                                                                                              |

### stac_version

In general, STAC versions can be mixed, but please keep the [recommended best practices](../best-practices.md#mixing-stac-versions) in mind.

### stac_extensions

A list of extensions the Item implements.
The list consists of URLs to JSON Schema files that can be used for validation.
This list must only contain extensions that extend the Item specification itself,
see the 'Scope' for each of the extensions.

### id

It is important that an Item identifier is unique within a Collection, and that the
[Collection identifier](../collection-spec/collection-spec.md#id) in turn is unique globally. Then the two can be combined to
give a globally unique identifier. Items are *[strongly recommended](#collections)* to have Collections, and not having one makes
it more difficult to be used in the wider STAC ecosystem.
If an Item does not have a Collection, then the Item identifier should be unique within its root Catalog or root Collection.

As most geospatial assets are already uniquely defined by some 
identification scheme from the data provider it is recommended to simply use that ID.
Data providers are advised to include sufficient information to make their IDs globally unique,
including things like unique satellite IDs.
See the [id section of best practices](../best-practices.md#item-ids) for additional recommendations.

### geometry

Defines the full footprint of the asset represented by this item, formatted according to RFC 7946.

If **a geometry** is provided, the value must be a Geometry Object according to
[RFC 7946, section 3.1](https://tools.ietf.org/html/rfc7946#section-3.1)
with the exception that the type `GeometryCollection` is not allowed in STAC.
If **no geometry** is provided, the value must be `null` according to
[RFC 7946, section 3.2](https://tools.ietf.org/html/rfc7946#section-3.2).

Coordinates are specified in Longitude/Latitude or Longitude/Latitude/Elevation based on [WGS 84](http://www.opengis.net/def/crs/OGC/1.3/CRS84).

### bbox

Bounding Box of the asset represented by this Item using either 2D or 3D geometries,
formatted according to [RFC 7946, section 5](https://tools.ietf.org/html/rfc7946#section-5).
The length of the array must be 2\*n where n is the number of dimensions.
The array contains all axes of the southwesterly most extent followed by all axes of the northeasterly most extent specified in
Longitude/Latitude or Longitude/Latitude/Elevation based on [WGS 84](http://www.opengis.net/def/crs/OGC/1.3/CRS84).
When using 3D geometries, the elevation of the southwesterly most extent is the minimum depth/height in meters
and the elevation of the northeasterly most extent is the maximum.
This field enables more naive clients to easily index and search geospatially.
STAC compliant APIs are required to compute intersection operations with the Item's geometry field, not its bbox.

### collection

The `id` of the STAC Collection this Item references to with the [`collection` relation type](#relation-types) in the `links`  array.

This field provides an easy way for a user to search for any Items that belong in a specified Collection.
If present, must be a non-empty string.

### properties

#### Properties Object

Additional metadata fields can be added to the GeoJSON Object Properties. The only required field
is `datetime` but it is recommended to add more fields, see [Additional Fields](#additional-fields)
resources below.

| Field Name | Type         | Description                                                                                                                                                                                                                                                                                                                                     |
| ---------- | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| datetime   | string\|null | **REQUIRED.** The searchable date and time of the assets, which must be in UTC. It is formatted according to [RFC 3339, section 5.6](https://tools.ietf.org/html/rfc3339#section-5.6). `null` is allowed, but requires `start_datetime` and `end_datetime` from [common metadata](../commons/common-metadata.md#date-and-time-range) to be set. |

#### datetime

This is likely the acquisition (in the case of single camera type captures) or the 'nominal'
or representative time in the case of assets that are combined together. Though time can be a
complex thing to capture, for this purpose keep in mind the STAC spec is primarily searching for
data, so use whatever single date and time is most useful for a user to search for. STAC content
extensions may further specify the meaning of the main `datetime` field, and many will also add more
datetime fields. **All times in STAC metadata should be in [Coordinated Universal 
Time](https://en.wikipedia.org/wiki/Coordinated_Universal_Time) (UTC).**
If there's clearly no meaningful single 'nominal' time, it is allowed to use `null` instead.
In this case it is **required** to specify a temporal interval with the fields `start_datetime`
and `end_datetime` from [common metadata](../commons/common-metadata.md#date-and-time-range). For example, if
your data is a time-series that covers 100 years, it's not very meaningful to set the datetime to a
single timestamp as it would not be found in most searches that searches for a decade of data in that
period although the Item actually covers the decade. See [datetime selection](../best-practices.md#datetime-selection)
in the best practices document for more information.

#### Additional Fields

Providers should include metadata fields that are relevant for users of STAC, but it is recommended
to [select only those necessary for search](../best-practices.md#field-selection-and-metadata-linking).
Where possible metadata fields should be mapped to the STAC Common Metadata and widely used extensions,
to enable cross-catalog search on known fields.

- [STAC Common Metadata](common-metadata.md#stac-common-metadata) - A list of fields commonly used
throughout all domains. These optional fields are included for STAC Items by default.
- [Extensions](../extensions/README.md) - Additional fields that are more specific,
such as [EO](https://github.com/stac-extensions/eo), [View](https://github.com/stac-extensions/view).
- [Custom Extensions](../extensions/README.md#extending-stac) - It is generally allowed to add custom
fields but it is recommended to add multiple fields for related values instead of a nested object,
e.g., two fields `view:azimuth` and `view:off_nadir` instead of a field `view` with an object
value containing the two fields. The convention (as used within Extensions) is for related fields 
to use a common prefix on the field names to group them, e.g. `view`. A nested data structure should
only be used when the data itself is nested, as with `bands`.

### Links

Each link in the `links` array must be a [Link Object](../commons/links.md#link-object).

#### Relation types

All [common relation types](../commons/links.md#relation-types) except for `item` can be used in Items.
A `self` and `collection` links are STRONGLY RECOMMENDED.
A link with this `rel` type is *required* for STAC item if the `collection` field in properties is present.

> \[!NOTE]
> Dynamic catalogs can implement multiple parents through a dynamic browsing interface as they could dynamically create the parent
> link based on the desired browsing structure (though only 1 parent at a time).
> Multiple parents are allowed for other types than `application/json`.

#### Collections

Items are *strongly recommended* to provide a link to a STAC Collection definition.
It is important as Collections provide additional information about a set of items,
for example the license, provider and other information
giving context on the overall set of data that an individual Item is a part of.

If Items are part of a STAC Collection, the
[STAC Collection spec *requires* Items to link back to the Collection](../collection-spec/collection-spec.md#relation-types).
Linking back must happen in two places:

1. The field `collection` in an Item must be filled (see section 'Item fields'). It is the `id` of a STAC Collection.
2. An Item must also provide a link to the STAC Collection using the [`collection` relation type](#relation-types):
   ```js
   "links": [
     { "rel": "collection", "href": "link/to/collection/record.json" }
   ]
   ```

Multiple collections can point to an Item, but an Item can only point back to a single collection.

### Assets

The property `assets` is a dictionary of [Asset Objects](../commons/assets.md#asset-object), each with a unique key.
Each asset refers to data associated with the Item that can be downloaded or streamed.
This construct is further detailed in the [Assets](../commons/assets.md) document.

Assets in a STAC Item should include the main asset, as well as any 'sidecar' files that are related and help a
client make sense of the data. Examples of this include extended metadata (in XML, JSON, etc.),
unusable data masks, satellite ephemeris data, etc. Some assets (like Landsat data) are represented
by multiple files - all should be linked to. It is generally recommended that different processing
levels or formats are not exhaustively listed in an Item, but instead are represented by related
Items that are linked to, but the best practices around this are still emerging.

It is STRONGLY RECOMMENDED to add to each STAC Item

- a thumbnail with the role `thumbnail` for preview purposes
- one or more data files with the role `data`

## Media Type for STAC Item

A STAC Item is a GeoJSON file ([RFC 7946](https://tools.ietf.org/html/rfc7946)), and thus should use the
[`application/geo+json`](https://tools.ietf.org/html/rfc7946#section-12) as the [Media Type](https://en.wikipedia.org/wiki/Media_type)
(previously known as the MIME Type).

## Extensions

STAC Items are [extensible](../extensions/README.md).
Please refer to the [extensions overview](https://stac-extensions.github.io) to find relevant extensions for STAC Items.
