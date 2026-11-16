-- The user's city, for the weather at the start of a call.
ALTER TABLE tenants ADD COLUMN city text;
ALTER TABLE tenants ADD COLUMN city_lat double precision;
ALTER TABLE tenants ADD COLUMN city_lon double precision;
