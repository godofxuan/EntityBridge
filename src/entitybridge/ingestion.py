"""Official-source adapters. Raw files and identifier-rich records stay outside matcher."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

PARSER_VERSION = "official-v2"
CH_TERMS = "https://www.gov.uk/government/publications/companies-house-accreditation-to-information-fair-traders-scheme/public-task-copyright-and-crown-copyright"
GLEIF_TERMS = "https://www.gleif.org/en/meta/lei-data-terms-of-use"


class OpaqueRegistry:
    """Private ingestion identity map; never mount this registry in a matcher process."""

    def __init__(self, path: Path):
        self.path = path
        self.entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def ids(self, source: str, source_key: str, content_hash: str) -> tuple[str, str]:
        key = json.dumps([source, source_key], separators=(",", ":"))
        entry = self.entries.setdefault(key, {"record_id": str(uuid4()), "versions": {}})
        version = entry["versions"].setdefault(content_hash, str(uuid4()))
        return entry["record_id"], version

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_suffix(".partial")
        partial.write_text(json.dumps(self.entries, sort_keys=True), encoding="utf-8")
        partial.replace(self.path)


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_snapshot(path: Path) -> dict:
    metadata = json.loads(path.with_suffix(path.suffix + '.manifest.json').read_text(encoding='utf-8'))
    if file_sha256(path) != metadata['sha256']:
        raise ValueError(f'Snapshot hash mismatch: {path.name}')
    return metadata


def entity_split(registry_entity: str, seed: int = 20260912) -> str:
    value = int.from_bytes(hashlib.sha256(f"split:{seed}:{registry_entity}".encode()).digest()[:8], "big") / 2**64
    return "train" if value < .6 else "validation" if value < .8 else "test"


def company_number(value: str) -> str | None:
    value = value.strip().upper()
    return value if re.fullmatch(r"[A-Z0-9]{8}", value) else None


def ch_country(value: str) -> str | None:
    mapping = {'ENGLAND':'GB','WALES':'GB','SCOTLAND':'GB','NORTHERN IRELAND':'GB','UNITED KINGDOM':'GB','GREAT BRITAIN':'GB',
               'JERSEY':'JE','GUERNSEY':'GG','ISLE OF MAN':'IM','PANAMA':'PA','CAYMAN ISLANDS':'KY','AUSTRALIA':'AU'}
    return mapping.get(value.strip().upper())


def parse_gleif(obj: dict, location: str) -> dict:
    attrs = obj["attributes"]
    entity = attrs["entity"]
    address = entity.get("legalAddress") or {}
    return {
        "source": "gleif", "source_key": obj["id"], "lei": obj["id"],
        "name": entity["legalName"]["name"],
        "address": " ".join(address.get("addressLines") or []),
        "city": address.get("city"), "postcode": address.get("postalCode"),
        "country": address.get("country"),
        "registration_authority": (entity.get("registeredAt") or {}).get("id"),
        "registration_number": company_number(entity.get("registeredAs") or ""),
        "registration_number_raw": entity.get("registeredAs"),
        "registration_status": attrs["registration"].get("status"),
        "status": entity.get("status"), "category": entity.get("category"),
        "other_names": entity.get("otherNames") or [],
        "headquarters_address": entity.get("headquartersAddress"),
        "associated_entity": entity.get("associatedEntity"),
        "successor_entities": entity.get("successorEntities") or [],
        "location": location,
    }


def gold_exclusion(record: dict) -> str | None:
    """Conservative, versioned gold scope; LAPSED is not treated as company dissolution."""
    expected = {"registration_authority": "RA000585", "country": "GB", "category": "GENERAL", "status": "ACTIVE", "registration_status": "ISSUED"}
    for key, value in expected.items():
        if record.get(key) != value:
            return f"{key}:{record.get(key)}"
    if not record.get("registration_number"):
        return "invalid_company_number"
    if not record.get("name"):
        return "missing_name"
    if record.get("successor_entities") or (record.get("associated_entity") or {}).get("lei"):
        return "identity_relationship"
    return None


def iter_gleif_csv_zip(path: Path, reject: Callable[[dict], None], stats: dict | None = None) -> Iterator[dict]:
    """Stream the full official Golden Copy; retain only the declared GB scope."""
    stats = stats if stats is not None else {}
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if not member.filename.lower().endswith('.csv'):
                continue
            with archive.open(member) as binary:
                reader = csv.reader(io.TextIOWrapper(binary, encoding='utf-8-sig', newline=''))
                headers = next(reader)
                required = {'LEI', 'Entity.LegalName', 'Entity.LegalAddress.Country', 'Entity.RegistrationAuthority.RegistrationAuthorityEntityID'}
                if not required.issubset(headers):
                    raise ValueError('Missing required GLEIF Golden Copy columns')
                stats['columns'] = headers
                country_index = headers.index('Entity.LegalAddress.Country')
                for values in reader:
                    stats['raw_rows'] = stats.get('raw_rows', 0) + 1
                    if len(values) != len(headers):
                        reject({'location': f'{member.filename}:{reader.line_num}', 'reason': 'invalid_csv_structure', 'raw': values})
                        continue
                    if values[country_index] != 'GB':
                        stats['excluded_non_gb'] = stats.get('excluded_non_gb', 0) + 1
                        continue
                    row = dict(zip(headers, values))
                    if not row['LEI'] or not row['Entity.LegalName']:
                        reject({'location': f'{member.filename}:{reader.line_num}', 'reason': 'missing_key_or_name', 'raw': row})
                        continue
                    def address(prefix, row=row):
                        return {'addressLines': [row[key] for key in [prefix+'.FirstAddressLine']+[prefix+f'.AdditionalAddressLine.{i}' for i in range(1,4)] if row.get(key)],
                                'city': row.get(prefix+'.City'), 'postalCode': row.get(prefix+'.PostalCode'), 'country': row.get(prefix+'.Country')}
                    entity = {'legalName': {'name':row['Entity.LegalName']},
                              'legalAddress':address('Entity.LegalAddress'), 'headquartersAddress':address('Entity.HeadquartersAddress'),
                              'registeredAt': {'id':row.get('Entity.RegistrationAuthority.RegistrationAuthorityID')},
                              'registeredAs': row.get('Entity.RegistrationAuthority.RegistrationAuthorityEntityID'),
                              'category':row.get('Entity.EntityCategory'), 'status':row.get('Entity.EntityStatus'),
                              'associatedEntity': {'lei':row.get('Entity.AssociatedEntity.AssociatedLEI')},
                              'successorEntities':[row[key] for key in row if 'Successor' in key and row[key]],
                              'otherNames':[{'name':row[f'Entity.OtherEntityNames.OtherEntityName.{i}'],
                                             'type':row.get(f'Entity.OtherEntityNames.OtherEntityName.{i}.type'),
                                             'language':row.get(f'Entity.OtherEntityNames.OtherEntityName.{i}.xmllang')}
                                            for i in range(1,6) if row.get(f'Entity.OtherEntityNames.OtherEntityName.{i}')]}
                    yield parse_gleif({'id':row['LEI'], 'attributes':{'entity':entity,'registration':{'status':row.get('Registration.RegistrationStatus')}}}, f'{member.filename}:{reader.line_num}')


def iter_ch_zip(path: Path, reject: Callable[[dict], None]) -> Iterator[dict]:
    """Stream CSV members without extracting paths or converting company numbers to ints."""
    with zipfile.ZipFile(path) as archive:
        members = [item for item in archive.infolist() if item.filename.lower().endswith(".csv")]
        if not members:
            raise ValueError("ZIP contains no CSV")
        for member in members:
            with archive.open(member) as binary:
                reader = csv.DictReader(io.TextIOWrapper(binary, encoding="utf-8-sig", newline=""))
                reader.fieldnames = [key.strip() for key in (reader.fieldnames or [])]
                if not {"CompanyName", "CompanyNumber"}.issubset(reader.fieldnames):
                    raise ValueError("Missing required Companies House columns")
                for row in reader:
                    location = f"{member.filename}:{reader.line_num}"
                    reason = None
                    number = company_number(row.get("CompanyNumber") or "")
                    if None in row or any(value is None for value in row.values()):
                        reason = "invalid_csv_structure"
                    elif not (row.get("CompanyName") or "").strip():
                        reason = "missing_name"
                    elif number is None:
                        reason = "invalid_company_number"
                    if reason:
                        reject({"location": location, "reason": reason, "raw": row})
                        continue
                    yield {
                        "source": "companies_house", "source_key": number,
                        "registration_authority": "RA000585", "registration_number": number,
                        "name": row["CompanyName"],
                        "address": " ".join(row.get(f"RegAddress.AddressLine{i}", "").strip() for i in (1, 2)).strip(),
                        "city": row.get("RegAddress.PostTown", ""),
                        "postcode": row.get("RegAddress.PostCode", ""),
                        "country": ch_country(row.get("RegAddress.Country", "")), "country_raw": row.get("RegAddress.Country", ""),
                        "status": row.get("CompanyStatus", ""),
                        "category": row.get("CompanyCategory", ""),
                        "other_names": [
                            {"name": row[f"PreviousName_{i}.CompanyName"], "valid_to": row.get(f"PreviousName_{i}.CONDATE"), "type": "PREVIOUS_LEGAL_NAME"}
                            for i in range(1, 11) if row.get(f"PreviousName_{i}.CompanyName")
                        ],
                        "location": location,
                    }
