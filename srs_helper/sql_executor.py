"""Restricted exploratory SQL. It cannot establish automatic feature conclusions."""
from pathlib import Path

import sqlglot
from sqlglot import exp

from .database import open_dataset
from .plans import default_plan, export_relation, prepare_scope


ALLOWED_FUNCTIONS = {"COUNT","AVG","MIN","MAX","SUM","STDDEV_SAMP","COALESCE","ROUND",
                     "ABS","CAST","DATE_TRUNC","QUANTILE_CONT","ROW_NUMBER","LAG","LEAD"}


def validate_sql(sql):
    if len(sql)>20000:
        raise ValueError("SQL exceeds 20,000 characters")
    trees = [t for t in sqlglot.parse(sql,read="duckdb") if t is not None]
    if len(trees)!=1 or not isinstance(trees[0],exp.Select):
        raise ValueError("Only one SELECT with optional CTEs is allowed")
    tree = trees[0]
    forbidden = (exp.Insert,exp.Update,exp.Delete,exp.Create,exp.Drop,exp.Command,exp.Into)
    for node in tree.walk():
        if isinstance(node,forbidden) or (isinstance(node,exp.With) and node.args.get("recursive")):
            raise ValueError("Writes, commands and recursive CTEs are not allowed")
        if isinstance(node,exp.Func):
            function = node.name.upper() if isinstance(node,exp.Anonymous) else node.sql_name()
            if function not in ALLOWED_FUNCTIONS:
                raise ValueError("Function is not allowlisted: "+function)
    relations = {"resource","stream"}
    def check_tables(node,allowed):
        for table in node.find_all(exp.Table):
            if not isinstance(table.this,exp.Identifier) or table.name not in allowed or table.db or table.catalog:
                raise ValueError("Only scoped resource/stream relations and earlier local CTEs may be read")
    # Non-recursive CTE self-reference can resolve a real database table in DuckDB.
    # Validate each CTE against earlier names, not the set of all declared names.
    with_nodes = list(tree.find_all(exp.With))
    root_with = tree.args.get("with") or tree.args.get("with_")
    if len(with_nodes)>1 or (with_nodes and with_nodes[0] is not root_with):
        raise ValueError("Nested WITH clauses are not supported")
    if root_with:
        for cte in root_with.expressions:
            if cte.alias_or_name in relations:
                raise ValueError("CTE shadows a controlled relation")
            check_tables(cte.this,relations)
            relations.add(cte.alias_or_name)
    check_tables(tree,relations)
    return tree.sql(dialect="duckdb")


def execute_sql(directory,scope,sql,parameters,output_dir):
    validated = validate_sql(sql)
    if not isinstance(parameters,list) or len(parameters)>100 or any(not isinstance(p,(str,int,float,bool,type(None))) for p in parameters):
        raise ValueError("Parameters must be a list of at most 100 scalar values")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True,exist_ok=True)
    with open_dataset(directory) as (con,meta):
        tree = sqlglot.parse_one(validated,read="duckdb")
        # A star explicitly requests all columns; COUNT(*) does not.
        all_fields = any(not isinstance(star.parent,exp.Count) for star in tree.find_all(exp.Star))
        fields = None if all_fields else {column.name for column in tree.find_all(exp.Column)}
        prepare_scope(con,scope,fields)
        con.execute("EXPLAIN "+validated,parameters)
        con.execute("CREATE TEMP TABLE manual_result AS "+validated,parameters)
        plan = default_plan(meta,"Manual exploratory SQL")
        artifact = export_relation(con,"manual_result",output_dir,plan,scope,"EXPLORATION_SQL")
        artifact.update(sql=validated,parameters=parameters,limitations=["仅探索性结果，不用于自动特性或根因判定"])
        return [artifact]
