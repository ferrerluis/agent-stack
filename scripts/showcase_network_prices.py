"""Selected public IPv4 internet traffic upper bounds from native catalogs."""
from decimal import Decimal
import hashlib
import json
import re
import time
from showcase_policy import require, money


def measure_network(attempt):
    if attempt.provider == 'aws':
        from showcase_aws_prices import AWSPriceQuery
        query=AWSPriceQuery(attempt.session.credentials)
        rows=query.products('AWSDataTransfer', {'fromLocation':'US East (N. Virginia)', 'toLocation':'External', 'transferType':'AWS Outbound'})
        rates=[]
        for row in rows:
            product=row.get('product',{})
            attrs=product.get('attributes',{})
            require(attrs.get('fromLocation')=='US East (N. Virginia)' and attrs.get('toLocation')=='External'
                    and attrs.get('transferType')=='AWS Outbound', 'network_price_scope_changed')
            for term in row.get('terms',{}).get('OnDemand',{}).values():
                for dimension in term.get('priceDimensions',{}).values():
                    require(dimension.get('unit')=='GB' and set(dimension.get('pricePerUnit',{}))=={'USD'}, 'network_price_unit_unsupported')
                    rates.append(money(dimension['pricePerUnit']['USD']))
        require(rates and max(rates)>0, 'network_price_missing')
        # AWS's GB can be bounded even under decimal-unit interpretation.
        rate=max(rates)*Decimal(1073741824)/1000000000
    else:
        from showcase_gcp_prices import CatalogHTTP, MAX_PAGES, MAX_TOTAL_BYTES
        http=CatalogHTTP()
        token=attempt.session.adapter.access_token()
        rows=[];page_token=None;seen=set();total=0;started=time.monotonic()
        for _ in range(MAX_PAGES):
            require(time.monotonic()-started<300,'network_price_measurement_expired')
            result,size,_=http.page(token,page_token)
            total+=size;require(total<=MAX_TOTAL_BYTES,'network_price_response_too_large')
            require(isinstance(result.get('skus'),list),'network_price_response_invalid')
            rows.extend(result['skus']);page_token=result.get('nextPageToken')
            if not page_token:break
            require(page_token not in seen,'network_price_pagination_invalid');seen.add(page_token)
        else:require(False,'network_price_pagination_limit')
        selected=[];rates=[]
        for row in rows:
            description=row.get('description','')
            if not re.fullmatch(r"Network Internet Data Transfer Out from .+", description):
                continue
            for entry in row.get('pricingInfo',[]):
                expression=entry.get('pricingExpression',{})
                require(expression.get('usageUnit')=='GiBy', 'network_price_unit_unsupported')
                for tier in expression.get('tieredRates',[]):
                    value=tier.get('unitPrice',{})
                    require(value.get('currencyCode')=='USD','network_price_unit_unsupported')
                    units=value.get('units','0');nanos=value.get('nanos',0)
                    require(isinstance(units,str) and type(nanos) is int and 0<=nanos<1000000000,'network_price_unit_unsupported')
                    rates.append(money(units)+Decimal(nanos)/1000000000)
            selected.append(row)
        require(selected and rates and max(rates)>0,'network_price_missing')
        rows=selected;rate=max(rates)
    require(0<rate<=1,'network_price_exceeds_supported_bound')
    return {'provider':attempt.provider,'status':'unit_price_measured','usd_per_gib_upper_bound':str(rate),
            'selection_sha256':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),
            'policy':'highest selected native USD tier; no free transfer allowance'}
