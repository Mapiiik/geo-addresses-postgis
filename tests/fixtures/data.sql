-- Seed rows, copied verbatim out of a real RUIAN / DGU import so the labels
-- the ranking is judged on are the genuine article. Every row is here to pin
-- down one specific behaviour; the comments say which.

INSERT INTO cz_addresses (
    kod_adm, obec_nazev, momc_nazev, mop_nazev, cast_obce_nazev, ulice_nazev, typ_so,
    cislo_domovni, cislo_orientacni, cislo_orientacni_znak, psc,
    geometry, formatted_address
) VALUES
-- The address behind the original bug report, and its village neighbours.
-- "Bozkovská" sorts first alphabetically, which is exactly how it used to win:
-- every label in the municipality tied on score and the tie-break decided.
(25510878, 'Vysoké nad Jizerou', NULL, NULL, 'Vysoké nad Jizerou', 'Věnceslava Metelky', 'č.p.', 367, NULL, NULL, 51211,
 ST_SetSRID(ST_MakePoint(15.39923, 50.68743), 4326), 'Věnceslava Metelky 367, 51211 Vysoké nad Jizerou'),
(16958608, 'Vysoké nad Jizerou', NULL, NULL, 'Vysoké nad Jizerou', 'Bozkovská', 'č.p.', 116, NULL, NULL, 51211,
 ST_SetSRID(ST_MakePoint(15.40031, 50.68586), 4326), 'Bozkovská 116, 51211 Vysoké nad Jizerou'),
(16958977, 'Vysoké nad Jizerou', NULL, NULL, 'Vysoké nad Jizerou', 'Bozkovská', 'č.p.', 153, NULL, NULL, 51211,
 ST_SetSRID(ST_MakePoint(15.39654, 50.68518), 4326), 'Bozkovská 153, 51211 Vysoké nad Jizerou'),

-- Numeric near-misses in the same village: 363/365/369 must never outrank 367,
-- and 36 must not be dragged in by "367" (nor 367 by a search for "36").
(16957814, 'Vysoké nad Jizerou', NULL, NULL, 'Vysoké nad Jizerou', NULL, 'č.p.', 36, NULL, NULL, 51211,
 ST_SetSRID(ST_MakePoint(15.40369, 50.68707), 4326), 'č.p. 36, 51211 Vysoké nad Jizerou'),
(25418327, 'Vysoké nad Jizerou', NULL, NULL, 'Vysoké nad Jizerou', NULL, 'č.p.', 363, NULL, NULL, 51211,
 ST_SetSRID(ST_MakePoint(15.39569, 50.68890), 4326), 'č.p. 363, 51211 Vysoké nad Jizerou'),
(25418297, 'Vysoké nad Jizerou', NULL, NULL, 'Vysoké nad Jizerou', NULL, 'č.p.', 365, NULL, NULL, 51211,
 ST_SetSRID(ST_MakePoint(15.40513, 50.68818), 4326), 'č.p. 365, 51211 Vysoké nad Jizerou'),
(25418271, 'Vysoké nad Jizerou', NULL, NULL, 'Vysoké nad Jizerou', NULL, 'č.p.', 369, NULL, NULL, 51211,
 ST_SetSRID(ST_MakePoint(15.39618, 50.68892), 4326), 'č.p. 369, 51211 Vysoké nad Jizerou'),

-- An evidence number ("č.ev.") in a part-of-municipality with no street: the
-- typ_so filter has to keep it apart from the č.p. numbers above, and the
-- envelope maps it to number_type "registration".
(16896670, 'Vysoké nad Jizerou', NULL, NULL, 'Helkovice', NULL, 'č.ev.', 31, NULL, NULL, 51301,
 ST_SetSRID(ST_MakePoint(15.38370, 50.65539), 4326), 'Helkovice č.ev. 31, 51301 Vysoké nad Jizerou'),

-- "Karlova" in Praha, and "Křesomyslova" — the pair that shows why street names
-- need word-aligned similarity: plain word_similarity scores the two at 0.375
-- on the shared "…slova" tail alone, which is over the threshold.
-- Praha is also the case where momc_nazev and mop_nazev are both populated and
-- identical, so the ladder's MOP rung answers before the MOMC one is reached.
(21715955, 'Praha', 'Praha 1', 'Praha 1', 'Staré Město', 'Karlova', 'č.p.', 144, 27, NULL, 11000,
 ST_SetSRID(ST_MakePoint(14.41910, 50.08644), 4326), 'Karlova 144/27, Staré Město, 11000 Praha 1'),
(21942854, 'Praha', 'Praha 4', 'Praha 4', 'Nusle', 'Křesomyslova', 'č.p.', 248, 5, NULL, 14000,
 ST_SetSRID(ST_MakePoint(14.43115, 50.06446), 4326), 'Křesomyslova 248/5, Nusle, 14000 Praha 4'),

-- Praha with an orientation letter, for the composite-number formatting.
(27808343, 'Praha', 'Praha 6', 'Praha 6', 'Dejvice', 'Studentská', 'č.p.', 1903, 14, 'a', 16000,
 ST_SetSRID(ST_MakePoint(14.39011, 50.10261), 4326), 'Studentská 1903/14a, Dejvice, 16000 Praha 6'),

-- Brno: a statutory city with a MOMC but no MOP, which is the only way the
-- ladder's MOMC rung is reachable — in Praha the MOP rung always fires first.
(19096810, 'Brno', 'Brno-střed', NULL, 'Brno-město', 'Masarykova', 'č.p.', 307, 30, NULL, 60200,
 ST_SetSRID(ST_MakePoint(16.61045, 49.19177), 4326), 'Masarykova 307/30, Brno-město, 60200 Brno'),

-- Composite number ("248/19": both halves must be findable on their own) and
-- 2480 next door, which "248" must not reach.
(11855321, 'Aš', NULL, NULL, 'Aš', 'Karlova', 'č.p.', 248, 19, NULL, 35201,
 ST_SetSRID(ST_MakePoint(12.18808, 50.22179), 4326), 'Karlova 248/19, 35201 Aš'),
(11869861, 'Aš', NULL, NULL, 'Aš', 'Karlova', 'č.p.', 2480, NULL, NULL, 35201,
 ST_SetSRID(ST_MakePoint(12.18753, 50.22366), 4326), 'Karlova 2480, 35201 Aš'),

-- Village address located by part-of-municipality rather than street.
(16901509, 'Jablonec nad Jizerou', NULL, NULL, 'Buřany', NULL, 'č.p.', 33, NULL, NULL, 51243,
 ST_SetSRID(ST_MakePoint(15.45115, 50.70490), 4326), 'Buřany 33, 51243 Jablonec nad Jizerou');

INSERT INTO hr_addresses (
    inspire_id, ulica, kucni_broj, naselje, postanski_broj, geometry
) VALUES
-- "Ilica 100" versus the 10000 postcode every Zagreb label carries: a house
-- number anchored only at its start matches the postcode too, which buried the
-- real hit under thousands of unrelated Zagreb addresses.
('HR.DGU.RPJ:KB.0022072614', 'Ilica', '100', 'Zagreb', 10000,
 ST_SetSRID(ST_MakePoint(15.96335, 45.81237), 4326)),
('HR.DGU.RPJ:KB.0022133647', 'Ilica', '45/1', 'Zagreb', 10000,
 ST_SetSRID(ST_MakePoint(15.96901, 45.81271), 4326)),
-- Letter-suffixed house numbers: searching "1" still has to reach "1A".
('HR.DGU.RPJ:KB.0022075271', 'Ilica', '1', 'Zagreb', 10000,
 ST_SetSRID(ST_MakePoint(15.97593, 45.81294), 4326)),
('HR.DGU.RPJ:KB.0022075272', 'Ilica', '1A', 'Zagreb', 10000,
 ST_SetSRID(ST_MakePoint(15.97574, 45.81294), 4326)),
-- "ulica" is Croatian for "street" and appears in a large share of all labels,
-- so it is the natural fuzzy near-match for a search for "Ilica".
('HR.DGU.RPJ:KB.0022044140', 'Novačka ulica', '100', 'Zagreb', 10040,
 ST_SetSRID(ST_MakePoint(16.03787, 45.83970), 4326)),
('HR.DGU.RPJ:KB.0022111689', 'Ulica breza', '100', 'Zagreb', 10040,
 ST_SetSRID(ST_MakePoint(16.07502, 45.82639), 4326)),
-- Diacritics-heavy street, used for the README's own example query.
('HR.DGU.RPJ:KB.0000601045', 'Stjepana Ivičevića', '7', 'Makarska', 21300,
 ST_SetSRID(ST_MakePoint(17.02338, 43.29218), 4326));

ANALYZE cz_addresses;
ANALYZE hr_addresses;
